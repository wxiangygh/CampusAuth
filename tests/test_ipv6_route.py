"""IPv6 直连路由的核心语义：探测命名空间、补定向解析、无 AAAA 时如实降级。

背景（2026-09-26 实测）：WARP 的 dns fallback 只匹配精确主机名，且它的上游
（校园 DNS + Cloudflare DoT）视图里常常没有 AAAA，浏览器因此永远只能建 IPv4
连接；能给出抖音 AAAA 的是 AliDNS 系解析器，所以「设为 IPv6 直连」必须同时
产生一条 NRPT 定向解析绑定，而不是往 hosts 文件里钉一个会过期的单节点。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import warp_exclusion
from core.config import configure_config


class PrefixTests(unittest.TestCase):
    def test_prefixes_cover_slash32_and_slash48(self):
        self.assertEqual(
            sorted(warp_exclusion._ipv6_prefixes_of(['2409:8087:5710:11:3000::67'])),
            ['2409:8087:5710::/48', '2409:8087::/32'])

    def test_namespace_probe_includes_representative_subdomains(self):
        # 根域名常常无记录（douyinpic.com 连 A 都没有），CDN 的 AAAA 挂在真实子域上
        observed = ['p3-pc.douyinpic.com', 'p3-sign.douyinpic.com', 'www.douyin.com',
                    'example.com']
        with patch.object(warp_exclusion, '_observed_names', return_value=observed):
            names = warp_exclusion._v6_namespace_names('douyinpic.com')
        self.assertEqual(names, ['douyinpic.com', 'www.douyinpic.com',
                                 'p3-pc.douyinpic.com', 'p3-sign.douyinpic.com'])

    def test_namespace_probe_caps_observed_subdomains(self):
        observed = [f'h{i}.douyinvod.com' for i in range(30)]
        with patch.object(warp_exclusion, '_observed_names', return_value=observed):
            names = warp_exclusion._v6_namespace_names('douyinvod.com')
        # 根域 + www + 最多 6 个观测子域，避免一次规则改动打出几十次 DNS 查询
        self.assertEqual(len(names), 8)
        self.assertEqual(names[:2], ['douyinvod.com', 'www.douyinvod.com'])


class ProbeV6Tests(unittest.TestCase):
    def test_probe_reports_system_gap_and_picks_server_with_aaaa(self):
        answers = {
            ('douyin.com', 'system'): [],
            ('www.douyin.com', 'system'): [],
            ('douyin.com', '114.114.114.114'): [],
            ('www.douyin.com', '114.114.114.114'): [],
            ('douyin.com', '223.5.5.5'): [],
            ('www.douyin.com', '223.5.5.5'): ['2409:8c54:10c0:40::31'],
        }

        def fake_query(name, rtype, nameservers):
            key = (name, 'system' if not nameservers else nameservers[0])
            return list(answers.get(key, []))

        with patch.object(warp_exclusion, '_query_records', fake_query), \
                patch.object(warp_exclusion, '_observed_names', return_value=[]), \
                patch.object(warp_exclusion, '_servers_for',
                             return_value=['114.114.114.114', '223.5.5.5']):
            system_addrs, server, addrs = warp_exclusion._probe_v6('douyin.com')
        self.assertEqual(system_addrs, [])
        self.assertEqual(server, '223.5.5.5')
        self.assertEqual(addrs, ['2409:8c54:10c0:40::31'])

    def test_probe_reports_no_server_when_no_configured_resolver_has_aaaa(self):
        with patch.object(warp_exclusion, '_query_records', return_value=[]), \
                patch.object(warp_exclusion, '_observed_names', return_value=[]), \
                patch.object(warp_exclusion, '_servers_for', return_value=['223.5.5.5']):
            self.assertEqual(warp_exclusion._probe_v6('example.com'), ([], '', []))


class DnsBindingTests(unittest.TestCase):
    def test_binding_is_created_once_and_never_overwrites_user_rule(self):
        import core.config as core_config
        # configure_config 会改写模块级 _store：借道 patch 让它测后复原，
        # 否则后面的用例会因为「store 已初始化」而走到不同分支
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(core_config, '_store', core_config._store):
            store = configure_config(Path(directory) / 'tray_config.json')
            note, created = warp_exclusion._ensure_v6_dns_binding('douyin.com', '223.5.5.5')
            self.assertTrue(created)
            self.assertIn('下发到系统', note)
            self.assertEqual(store.snapshot()['dns_bindings'],
                             [{'target': 'douyin.com', 'servers': ['223.5.5.5'],
                               'enabled': True}])
            # 已有绑定（可能是用户自己填的服务器）不得被覆盖
            note2, created2 = warp_exclusion._ensure_v6_dns_binding('douyin.com', '114.114.114.114')
            self.assertFalse(created2)
            self.assertEqual(note2, '')
            self.assertEqual(store.snapshot()['dns_bindings'][0]['servers'], ['223.5.5.5'])


class WarpAddHostIpv6Tests(unittest.TestCase):
    def _run(self, probe, binding=('需下发定向解析', True)):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(' '.join(str(part) for part in cmd))
            return 0, '', ''

        with patch.object(warp_exclusion, 'get_warp_cli_path', return_value='warp-cli'), \
                patch.object(warp_exclusion, 'run_command_simple', fake_run), \
                patch.object(warp_exclusion, '_probe_v6', return_value=probe), \
                patch.object(warp_exclusion, '_resolve_ipv4_addresses',
                             return_value=['39.136.116.228']), \
                patch.object(warp_exclusion, '_add_ipv4_firewall_block',
                             return_value=(True, ['39.136.116.0/24'])), \
                patch.object(warp_exclusion, '_ensure_v6_dns_binding',
                             return_value=binding) as bind, \
                patch.object(warp_exclusion, '_remove_ipv6_hosts_entry',
                             return_value=(True, 'hosts 中无该域名条目')) as remove, \
                patch.object(warp_exclusion, 'flush_dns_cache'):
            ok, msg, blocked = warp_exclusion.warp_add_host('douyin.com', route='ipv6')
        return ok, msg, blocked, calls, bind, remove

    def test_ipv6_route_excludes_warp_and_reports_pending_nrpt(self):
        probe = ([], '223.5.5.5', ['2409:8087:5710:11:3000::67'])
        ok, msg, blocked, calls, bind, remove = self._run(probe)
        self.assertTrue(ok)
        self.assertIn('需下发定向解析', msg)
        self.assertEqual(blocked, ['39.136.116.0/24'])
        self.assertIn('warp-cli tunnel host add douyin.com', calls)
        self.assertIn('warp-cli tunnel host add *.douyin.com', calls)
        self.assertIn('warp-cli tunnel ip add-range 2409:8087::/32', calls)
        bind.assert_called_once_with('douyin.com', '223.5.5.5')
        # 定向解析接管后，hosts 钉单节点的做法要清掉
        remove.assert_called_once_with('douyin.com')

    def test_ipv6_route_keeps_existing_binding_when_system_already_has_aaaa(self):
        probe = (['2408:872b:e01:14:8000:0:b00:99'], '223.5.5.5',
                 ['2408:872b:e01:14:8000:0:b00:99'])
        ok, msg, _blocked, _calls, bind, remove = self._run(probe)
        self.assertTrue(ok)
        self.assertEqual(msg, '添加成功')
        bind.assert_not_called()
        remove.assert_not_called()

    def test_no_aaaa_anywhere_downgrades_instead_of_claiming_ipv6(self):
        ok, msg, _blocked, calls, bind, _remove = self._run(([], '', []))
        self.assertTrue(ok)
        self.assertIn('无 IPv6 地址', msg)
        self.assertIn('IPv4 直连', msg)
        bind.assert_not_called()
        self.assertFalse([c for c in calls if 'add-range' in c])


class ApplyToWarpTests(unittest.TestCase):
    def test_apply_writes_downgrade_back_into_config(self):
        """列表显示 IPv6 而实际走 IPv4 是这次排查的起点，必须钉住。"""
        with tempfile.TemporaryDirectory() as directory:
            cfg_file = Path(directory) / 'warp_exclusion_config.json'
            cfg_file.write_text(
                '{"domains": [{"domain": "v26-weba.douyinvod.com", "route": "ipv6",'
                ' "enabled": true}], "ip_ranges": [], "dns_fallback": []}',
                encoding='utf-8')
            import json
            with patch.object(warp_exclusion, 'EXCLUSION_CONFIG_FILE', cfg_file), \
                    patch.object(warp_exclusion.ExclusionManager, 'sync_from_warp',
                                 lambda self, kind=None: (True, '', [])), \
                    patch.object(warp_exclusion, 'warp_add_host',
                                 return_value=(True,
                                               '域名 v26-weba.douyinvod.com 无 IPv6 地址，'
                                               '已自动改为 IPv4 直连模式（WARP 排除+IPv4 直连）',
                                               [])):
                mgr = warp_exclusion.ExclusionManager()
                ok, msg, _details = mgr.apply_to_warp()
            self.assertTrue(ok)
            self.assertIn('降级为 IPv4', msg)
            saved = json.loads(cfg_file.read_text(encoding='utf-8'))
            self.assertEqual(saved['domains'][0]['route'], 'ipv4')


if __name__ == '__main__':
    unittest.main()
