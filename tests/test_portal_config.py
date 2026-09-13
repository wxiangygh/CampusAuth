"""Portal 节点自定义配置相关单测：params 持久化、有线/无线检测、参数解析。"""
import sys
import unittest
from unittest import mock

from core.auth_workflow import (_apply_deferred_link_exclusive, _interface,
                                _plan_link_isolation, _resolve_portal_params,
                                _resolve_workflow_link, _server_to_ip_port)
from core.network import (_wlan_connected, _list_physical_adapters,
                          apply_link_exclusive, detect_link_type)
from core.workflow import StepSpec, WorkflowContext


class StepSpecParamsTests(unittest.TestCase):
    """StepSpec.from_dict 对 params 的保留与清洗。"""

    def test_params_kept_and_roundtrip(self):
        params = {'link_mode': 'auto', 'wireless': {'method': 'web', 'auth_url': 'http://x/y'},
                  'wired': {'method': 'http', 'server': '1.2.3.4:801'}}
        step = StepSpec.from_dict({'id': 'portal_login', 'params': params})
        self.assertEqual(step.params, params)
        # from_dict → as_dict 形式回传后仍保留
        again = StepSpec.from_dict({'id': 'portal_login', 'params': step.params})
        self.assertEqual(again.params, params)

    def test_missing_or_invalid_params_become_empty_dict(self):
        self.assertEqual(StepSpec.from_dict({'id': 'x'}).params, {})
        self.assertEqual(StepSpec.from_dict({'id': 'x', 'params': None}).params, {})
        self.assertEqual(StepSpec.from_dict({'id': 'x', 'params': ['bad']}).params, {})


class ClickStepsTests(unittest.TestCase):
    """多步点击列表：规范化、兼容回退、参数解析透传。"""

    def test_normalize_keeps_valid_and_defaults_contains(self):
        from core.portal_web import normalize_click_steps
        steps = normalize_click_steps([
            {'name': '注销', 'match': 'exact'},
            {'name': '确定'},
            {'name': '  ', 'match': 'exact'},       # 空名剔除
            {'name': '退出登录', 'match': 'bad'},    # 非法 match 归位 contains
            '字符串项',                               # 宽容：等价 {name, contains}
            {'no_name': 1},                           # 无名剔除
        ])
        self.assertEqual(steps, [
            {'name': '注销', 'match': 'exact'},
            {'name': '确定', 'match': 'contains'},
            {'name': '退出登录', 'match': 'contains'},
            {'name': '字符串项', 'match': 'contains'},
        ])

    def test_normalize_rejects_non_list_and_caps_length(self):
        from core.portal_web import normalize_click_steps, _MAX_STEPS
        self.assertEqual(normalize_click_steps(None), [])
        self.assertEqual(normalize_click_steps('登录'), [])
        steps = normalize_click_steps(
            [{'name': f'按钮{i}'} for i in range(_MAX_STEPS + 3)])
        self.assertEqual(len(steps), _MAX_STEPS)

    def test_build_falls_back_to_button_name(self):
        from core.portal_web import build_click_steps
        self.assertEqual(
            build_click_steps([], '登录'),
            [{'name': '登录', 'match': 'contains'}])
        self.assertEqual(
            build_click_steps([{'name': '注销', 'match': 'exact'}], '登录'),
            [{'name': '注销', 'match': 'exact'}])  # steps 优先于旧字段
        self.assertEqual(build_click_steps(None, ''), [])  # 双空 → 兜底提交

    def test_step_aliases_split(self):
        from core.portal_web import _step_aliases
        self.assertEqual(_step_aliases('登录, 认证、login'), ['登录', '认证', 'login'])

    def test_resolve_portal_params_passes_click_steps(self):
        with mock.patch('core.network.detect_link_type', return_value='wireless'):
            context = WorkflowContext(config={}, cancelled=lambda: False)
            context.data['link_type'] = 'wireless'
            step = StepSpec.from_dict({'id': 'portal_logout', 'params': {
                'link_mode': 'wireless',
                'wireless': {'method': 'web', 'auth_url': 'http://w',
                             'click_steps': [
                                 {'name': '注销', 'match': 'exact'},
                                 {'name': '确定'},
                             ]}}})
            resolved = _resolve_portal_params(step, context)
        self.assertEqual(resolved['click_steps'], [
            {'name': '注销', 'match': 'exact'},
            {'name': '确定', 'match': 'contains'},
        ])
        self.assertEqual(resolved['button_name'], '')

    def test_auto_empty_wireless_falls_back_to_configured_wired(self):
        """自动检测选中一套"空配置"（无线 HTTP 无服务器、只有旧按钮名残留）
        时必须回退另一套已配置变体——用户配置在有线，无线没配，
        自动检测不应跑成对全局服务器的 HTTP 注销。"""
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wireless'
        step = StepSpec.from_dict({'id': 'portal_logout', 'params': {
            'link_mode': 'auto',
            'wireless': {'server': '', 'method': 'http', 'auth_url': '',
                         'button_name': '本机注销'},
            'wired': {'method': 'web', 'auth_url': 'https://p/x.htm',
                      'button_name': '本机注销',
                      'click_steps': [{'name': '本机注销'}, {'name': '确定'}]}}})
        resolved = _resolve_portal_params(step, context)
        self.assertEqual(resolved['link'], 'wireless')      # 链路判定不变
        self.assertEqual(resolved['method'], 'web')          # 但回退到有线变体
        self.assertEqual(resolved['auth_url'], 'https://p/x.htm')
        self.assertEqual([s['name'] for s in resolved['click_steps']],
                         ['本机注销', '确定'])

    def test_auto_http_without_server_counts_as_unconfigured(self):
        """http 变体仅当服务器非空才算配置过；空服务器=回退全局，在自动
        检测场景不能算"已配置"。"""
        from core.auth_workflow import _portal_variant_configured
        self.assertFalse(_portal_variant_configured(
            {'method': 'http', 'server': '', 'button_name': '本机注销'}))
        self.assertTrue(_portal_variant_configured(
            {'method': 'http', 'server': '10.0.0.1:801'}))
        self.assertTrue(_portal_variant_configured(
            {'method': 'web', 'button_name': '登录'}))

    def test_explicit_link_mode_also_falls_back_when_unconfigured(self):
        """显式指定链路但该变体未配置时同样回退另一套（显式选择在两套
        都配置时才起区分作用）。"""
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wireless'
        step = StepSpec.from_dict({'id': 'portal_login', 'params': {
            'link_mode': 'wireless',
            'wireless': {'server': '', 'method': 'http'},
            'wired': {'method': 'web', 'auth_url': 'https://p/x.htm'}}})
        resolved = _resolve_portal_params(step, context)
        self.assertEqual(resolved['method'], 'web')
        self.assertEqual(resolved['auth_url'], 'https://p/x.htm')


@unittest.skipUnless(sys.platform == 'win32', '需要 WebView2 的真机端到端')
class PortalStepsEndToEndTests(unittest.TestCase):
    """多步点击真机冒烟：本地两步注销页面（注销→确认层→注销成功）。"""

    PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>p</title></head>
<body>
<button id="logout">注销</button>
<div id="panel" style="display:none">
  <p>确定要退出当前会话吗？</p>
  <!-- 网页内确认层的按钮常是绑定事件的 <a>（layui-layer 风格），
       无 role/onclick 属性，传统按钮选择器扫不到 —— 深扫描回退的回归点 -->
  <a class="layui-layer-btn0" id="confirm">确定</a>
  <a class="layui-layer-btn1">取消</a>
</div>
<p id="status"></p>
<script>
document.getElementById('logout').onclick=function(){
  document.getElementById('panel').style.display='block';};
document.getElementById('confirm').addEventListener('click',function(){
  document.getElementById('status').innerText='注销成功，会话已结束';});
</script>
</body></html>"""

    def test_two_step_logout_with_page_change_gate(self):
        import http.server
        import threading

        import webview

        from core.portal_web import web_portal_submit

        page = self.PAGE

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self2):
                data = page.encode('utf-8')
                self2.send_response(200)
                self2.send_header('Content-Type', 'text/html; charset=utf-8')
                self2.send_header('Content-Length', str(len(data)))
                self2.end_headers()
                self2.wfile.write(data)

            def log_message(self2, *a):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        holder = webview.create_window('test-holder', html='<p>hold</p>',
                                       hidden=True)
        results = {}

        def run():
            try:
                results['r'] = web_portal_submit(
                    f'http://127.0.0.1:{port}/', '', '', '',
                    click_steps=[{'name': '注销', 'match': 'exact'},
                                 {'name': '确定', 'match': 'exact'}],
                    timeout=25)
            finally:
                holder.destroy()

        webview.start(run)
        success, message = results.get('r', (False, '未执行'))
        self.assertTrue(success, message)
        self.assertIn('认证成功', message)


class DetectLinkTypeTests(unittest.TestCase):
    """detect_link_type：优先无线的简单启发式。"""

    def test_wlan_connected_returns_wireless(self):
        netsh = '    状态    : 已连接\n    SSID    : CampusWiFi'
        with mock.patch('core.network.run_command', return_value=(0, netsh, '')):
            self.assertEqual(detect_link_type(), 'wireless')

    def test_wlan_disconnected_with_wired_adapter_returns_wired(self):
        netsh = '    状态    : 已断开连接'
        # _list_physical_adapters 解析格式：Name|Status（Get-NetAdapter -Physical）
        adapters = '以太网|Up\n以太网 2|Up'
        def fake_run(cmd, **kw):
            return (0, adapters, '') if isinstance(cmd, list) else (0, netsh, '')
        with mock.patch('core.network.run_command', side_effect=fake_run):
            self.assertEqual(detect_link_type(), 'wired')

    def test_nothing_detected_falls_back_to_wireless(self):
        netsh = '    状态    : 已断开连接'
        def fake_run(cmd, **kw):
            return (0, '', '') if isinstance(cmd, list) else (0, netsh, '')
        with mock.patch('core.network.run_command', side_effect=fake_run):
            self.assertEqual(detect_link_type(), 'wireless')

    def test_disconnected_state_line_not_misread_as_connected(self):
        # '已断开连接' 包含 '连接' 但不是已连接
        self.assertFalse(_wlan_connected('    状态    : 已断开连接'))
        # 真实连接 = 状态已连接 且 SSID 非空（netsh 输出中 SSID 在状态之后）
        self.assertTrue(_wlan_connected(
            '    State    : connected\n    SSID    : CampusNet'))
        # 已连接但无 SSID（Wi-Fi Direct 虚拟接口）不算无线联网
        self.assertFalse(_wlan_connected('    State    : connected'))
        self.assertFalse(_wlan_connected(
            '    状态    : 已连接\n    SSID    : '))

    def test_list_physical_adapters_parses_name_status(self):
        """有线探测命令（Get-NetAdapter -Physical，Status 为属性输出）解析。"""
        def fake_run(cmd, **kw):
            # 假输出模拟 PS 端 Where-Object 过滤后的结果（仅 802.3 适配器）
            return (0, '以太网|Up\n以太网 2|Up', '')
        with mock.patch('core.network.run_command', side_effect=fake_run):
            adapters = _list_physical_adapters('802\\.3')
        self.assertEqual(adapters, [('以太网', 'Up'), ('以太网 2', 'Up')])


class ResolvePortalParamsTests(unittest.TestCase):
    """_resolve_portal_params：link_mode 选择、变体回退、默认值。"""

    def test_empty_params_fallback_http(self):
        # 空 params（内置工作流）→ http 方式，行为与旧版一致
        step = StepSpec.from_dict({'id': 'portal_login'})
        resolved = _resolve_portal_params(step, {})
        self.assertEqual(resolved['method'], 'http')
        self.assertEqual(resolved['server'], '')

    def test_link_mode_auto_uses_detected_link(self):
        step = StepSpec.from_dict({'id': 'portal_login', 'params': {
            'link_mode': 'auto',
            'wireless': {'method': 'web', 'auth_url': 'http://w'},
            'wired': {'method': 'http', 'server': '1.1.1.1:80'}}})
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wired'):
            resolved = _resolve_portal_params(step, {})
        self.assertEqual(resolved['link'], 'wired')
        self.assertEqual(resolved['method'], 'http')
        self.assertEqual(resolved['server'], '1.1.1.1:80')

    def test_link_mode_auto_prefers_prescanned_link(self):
        # 预扫描结果（context.data['link_type']）优先于现场探测，避免二次探测不一致
        step = StepSpec.from_dict({'id': 'portal_login', 'params': {'link_mode': 'auto'}})
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wired'
        resolved = _resolve_portal_params(step, context)
        self.assertEqual(resolved['link'], 'wired')

    def test_link_mode_forced_skips_detection(self):
        step = StepSpec.from_dict({'id': 'portal_login', 'params': {
            'link_mode': 'wireless',
            'wireless': {'method': 'web', 'auth_url': 'http://w', 'button_name': '登录'}}})
        with mock.patch('core.network.detect_link_type') as det:
            resolved = _resolve_portal_params(step, {})
            det.assert_not_called()
        self.assertEqual(resolved['link'], 'wireless')
        self.assertEqual(resolved['method'], 'web')
        self.assertEqual(resolved['auth_url'], 'http://w')
        self.assertEqual(resolved['button_name'], '登录')

    def test_missing_variant_falls_back_to_other(self):
        # 只配了 wireless，auto 判定为 wired 时回退 wireless
        step = StepSpec.from_dict({'id': 'portal_login', 'params': {
            'link_mode': 'auto',
            'wireless': {'method': 'web', 'auth_url': 'http://w'}}})
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wired'):
            resolved = _resolve_portal_params(step, {})
        self.assertEqual(resolved['method'], 'web')
        self.assertEqual(resolved['auth_url'], 'http://w')

    def test_invalid_method_falls_back_to_http(self):
        step = StepSpec.from_dict({'id': 'portal_login', 'params': {
            'wireless': {'method': 'weird'}}})
        self.assertEqual(_resolve_portal_params(step, {})['method'], 'http')


class ServerToIpPortTests(unittest.TestCase):
    """_server_to_ip_port：'ip:port' 字符串解析。"""

    def test_plain(self):
        self.assertEqual(_server_to_ip_port('10.21.221.98:801'),
                         {'portal_ip': '10.21.221.98', 'portal_port': '801'})

    def test_empty_returns_no_override(self):
        self.assertEqual(_server_to_ip_port(''), {})
        self.assertEqual(_server_to_ip_port('   '), {})

    def test_strips_scheme_and_path(self):
        self.assertEqual(_server_to_ip_port('http://10.0.0.1:80/eportal/x'),
                         {'portal_ip': '10.0.0.1', 'portal_port': '80'})

    def test_no_port_gives_empty_port(self):
        # 无端口时置空 portal_port，由 core.auth 的全局默认端口兜底
        self.assertEqual(_server_to_ip_port('10.0.0.1'),
                         {'portal_ip': '10.0.0.1', 'portal_port': ''})

    def test_domain_without_port(self):
        # 域名 + 无端口：portal_port 置空，避免全局端口误加到用户域名上
        self.assertEqual(_server_to_ip_port('www.baidu.com'),
                         {'portal_ip': 'www.baidu.com', 'portal_port': ''})

    def test_domain_with_port(self):
        self.assertEqual(_server_to_ip_port('portal.example.com:8080'),
                         {'portal_ip': 'portal.example.com', 'portal_port': '8080'})


class ResolveWorkflowLinkTests(unittest.TestCase):
    """_resolve_workflow_link：整条工作流的链路类型预扫描。"""

    def test_explicit_link_mode_wins(self):
        steps = [{'id': 'enable_ipv4'},
                 {'id': 'portal_login', 'params': {'link_mode': 'wired'}}]
        link, explicit = _resolve_workflow_link(steps)
        self.assertEqual((link, explicit), ('wired', True))

    def test_auto_falls_back_to_detection(self):
        steps = [{'id': 'portal_login', 'params': {'link_mode': 'auto'}}]
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wired'):
            link, explicit = _resolve_workflow_link(steps)
        self.assertEqual((link, explicit), ('wired', False))

    def test_login_node_decides_logout_ignored(self):
        # 认证链路只看 portal_login：注销节点（按当前链路执行）的 link_mode
        # 不再代表工作流链路——否则「注销(auto)+认证(无线)」会被预扫描成有线
        steps = [{'id': 'portal_logout', 'params': {'link_mode': 'wireless'}},
                 {'id': 'portal_login', 'params': {'link_mode': 'wired'}}]
        link, explicit = _resolve_workflow_link(steps)
        self.assertEqual((link, explicit), ('wired', True))

    def test_reauth_logout_auto_login_wireless(self):
        # 用户场景：原为有线，选了无线认证。预扫描必须取认证节点的 wireless，
        # 才能在注销（按当前有线）后做无线隔离并连接 WiFi
        steps = [{'id': 'portal_logout', 'params': {'link_mode': 'auto'}},
                 {'id': 'portal_login', 'params': {'link_mode': 'wireless'}}]
        link, explicit = _resolve_workflow_link(steps)
        self.assertEqual((link, explicit), ('wireless', True))

    def test_pure_logout_workflow_is_always_auto(self):
        # 纯注销工作流：注销节点不参与预扫描 → 一律现场检测（不显式隔离）
        steps = [{'id': 'portal_logout', 'params': {'link_mode': 'wireless'}}]
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wired'):
            link, explicit = _resolve_workflow_link(steps)
        self.assertEqual((link, explicit), ('wired', False))

    def test_no_portal_node_uses_detection(self):
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wireless'):
            link, explicit = _resolve_workflow_link([{'id': 'enable_ipv4'}])
        self.assertEqual((link, explicit), ('wireless', False))

    def test_bad_link_mode_treated_as_auto(self):
        steps = [{'id': 'portal_login', 'params': {'link_mode': 'weird'}}]
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wireless'):
            link, explicit = _resolve_workflow_link(steps)
        self.assertEqual((link, explicit), ('wireless', False))


class LogoutFollowsCurrentLinkTests(unittest.TestCase):
    """注销按「当前实际链路」选变体：无视 link_mode 与预扫描（follow_current_link）。

    Portal 会话绑定当前链路的 IP：用户有线在线时选了无线工作流，
    注销执行的也必须是有线的注销；反之同理。
    """

    def test_logout_wired_current_ignores_wireless_mode(self):
        step = StepSpec.from_dict({'id': 'portal_logout', 'params': {
            'link_mode': 'wireless',
            'wireless': {'method': 'web', 'auth_url': 'http://w'},
            'wired': {'method': 'http', 'server': '10.0.0.1:801'}}})
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wired'):
            resolved = _resolve_portal_params(step, {}, follow_current_link=True)
        self.assertEqual(resolved['link'], 'wired')
        self.assertEqual(resolved['method'], 'http')
        self.assertEqual(resolved['server'], '10.0.0.1:801')

    def test_logout_wireless_current_ignores_wired_mode(self):
        step = StepSpec.from_dict({'id': 'portal_logout', 'params': {
            'link_mode': 'wired',
            'wired': {'method': 'web', 'auth_url': 'http://wd'},
            'wireless': {'method': 'web', 'auth_url': 'http://wl'}}})
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wireless'):
            resolved = _resolve_portal_params(step, {}, follow_current_link=True)
        self.assertEqual(resolved['link'], 'wireless')
        self.assertEqual(resolved['auth_url'], 'http://wl')

    def test_logout_ignores_prescanned_link_too(self):
        # 预扫描写了 wireless（来自工作流显式 link_mode），当前实际有线：
        # 注销仍按现场检测的有线执行
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wireless'
        step = StepSpec.from_dict({'id': 'portal_logout', 'params': {
            'wireless': {'method': 'web', 'auth_url': 'http://w'},
            'wired': {'method': 'http', 'server': '10.0.0.1:801'}}})
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wired'):
            resolved = _resolve_portal_params(step, context, follow_current_link=True)
        self.assertEqual(resolved['link'], 'wired')

    def test_logout_unconfigured_current_variant_still_falls_back(self):
        # 当前链路的变体未配置时仍回退另一套（保证注销可执行）
        step = StepSpec.from_dict({'id': 'portal_logout', 'params': {
            'wireless': {'method': 'web', 'auth_url': 'http://w'}}})
        with mock.patch('core.auth_workflow.detect_link_type', return_value='wired'):
            resolved = _resolve_portal_params(step, {}, follow_current_link=True)
        self.assertEqual(resolved['link'], 'wired')
        self.assertEqual(resolved['method'], 'web')
        self.assertEqual(resolved['auth_url'], 'http://w')

    def test_login_default_keeps_explicit_mode(self):
        # 认证节点（follow_current_link 默认 False）行为不变：显式链路直接生效
        step = StepSpec.from_dict({'id': 'portal_login', 'params': {
            'link_mode': 'wireless',
            'wireless': {'method': 'web', 'auth_url': 'http://w'}}})
        with mock.patch('core.auth_workflow.detect_link_type') as det:
            resolved = _resolve_portal_params(step, {}, follow_current_link=False)
        det.assert_not_called()
        self.assertEqual(resolved['link'], 'wireless')


class PlanLinkIsolationTests(unittest.TestCase):
    """_plan_link_isolation：纯注销不隔离；注销+重认证链路不一致时推迟。"""

    def test_explicit_auth_only_applies_now(self):
        wf = [{'id': 'portal_login', 'params': {'link_mode': 'wired'}}]
        self.assertEqual(_plan_link_isolation(wf, 'wired', True, 'wired'), 'now')
        self.assertEqual(_plan_link_isolation(wf, 'wired', True, 'wireless'), 'now')

    def test_auto_never_isolates(self):
        wf = [{'id': 'portal_login'}]
        self.assertEqual(_plan_link_isolation(wf, 'wireless', False, 'wireless'), 'none')

    def test_pure_logout_never_isolates(self):
        # 纯注销工作流：即使显式指定链路也不动网卡（隔离会把正在使用的
        # 链路禁掉，注销请求本身都发不出去）
        wf = [{'id': 'portal_logout', 'params': {'link_mode': 'wireless'}}]
        self.assertEqual(_plan_link_isolation(wf, 'wireless', True, 'wired'), 'none')
        self.assertEqual(_plan_link_isolation(wf, 'wireless', True, 'wireless'), 'none')

    def test_reauth_mismatch_defers(self):
        wf = [{'id': 'portal_logout'},
              {'id': 'portal_login', 'params': {'link_mode': 'wireless'}}]
        self.assertEqual(_plan_link_isolation(wf, 'wireless', True, 'wired'), 'defer')

    def test_reauth_match_applies_now(self):
        wf = [{'id': 'portal_logout'},
              {'id': 'portal_login', 'params': {'link_mode': 'wired'}}]
        self.assertEqual(_plan_link_isolation(wf, 'wired', True, 'wired'), 'now')

    def test_disabled_logout_counts_as_absent(self):
        wf = [{'id': 'portal_logout', 'enabled': False},
              {'id': 'portal_login', 'params': {'link_mode': 'wired'}}]
        self.assertEqual(_plan_link_isolation(wf, 'wired', True, 'wireless'), 'now')


class DeferredLinkExclusiveTests(unittest.TestCase):
    """_apply_deferred_link_exclusive：注销完成后应用被推迟的链路隔离。"""

    def test_applies_pending_and_clears(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['pending_link_exclusive'] = 'wired'
        with mock.patch('core.auth_workflow.apply_link_exclusive',
                        return_value=(True, '已切换')) as excl:
            _apply_deferred_link_exclusive(context)
        excl.assert_called_once_with('wired')
        self.assertNotIn('pending_link_exclusive', context.data)

    def test_noop_without_pending(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        with mock.patch('core.auth_workflow.apply_link_exclusive') as excl:
            _apply_deferred_link_exclusive(context)
        excl.assert_not_called()

    def test_invalid_pending_ignored(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['pending_link_exclusive'] = 'auto'
        with mock.patch('core.auth_workflow.apply_link_exclusive') as excl:
            _apply_deferred_link_exclusive(context)
        excl.assert_not_called()

    def test_failure_does_not_raise(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['pending_link_exclusive'] = 'wireless'
        with mock.patch('core.auth_workflow.apply_link_exclusive',
                        return_value=(False, '未找到无线网卡')):
            _apply_deferred_link_exclusive(context)  # 不应抛异常
        self.assertNotIn('pending_link_exclusive', context.data)

    def test_cancelled_run_discards_pending(self):
        # 运行已取消：不再切换网卡，把机器留在当前链路上
        context = WorkflowContext(config={}, cancelled=lambda: True)
        context.data['pending_link_exclusive'] = 'wireless'
        with mock.patch('core.auth_workflow.apply_link_exclusive') as excl:
            _apply_deferred_link_exclusive(context)
        excl.assert_not_called()
        self.assertNotIn('pending_link_exclusive', context.data)


class InterfaceByLinkTests(unittest.TestCase):
    """_interface：IPv4/IPv6 等节点按链路类型选网卡。"""

    def test_wired_link_uses_wired_adapter(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wired'
        with mock.patch('core.auth_workflow.get_wired_interface_name',
                        return_value='以太网 2'):
            name, error = _interface(context)
        self.assertIsNone(error)
        self.assertEqual(name, '以太网 2')
        self.assertEqual(context.data['interface_name'], '以太网 2')

    def test_wired_link_missing_adapter_fails(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wired'
        with mock.patch('core.auth_workflow.get_wired_interface_name', return_value=None):
            name, error = _interface(context)
        self.assertIsNone(name)
        self.assertEqual(error.code, 'interface_missing')

    def test_wireless_link_keeps_legacy_behavior(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wireless'
        with mock.patch('core.auth_workflow.get_wifi_interface_name', return_value='WLAN'):
            name, error = _interface(context)
        self.assertIsNone(error)
        self.assertEqual(name, 'WLAN')


class ApplyLinkExclusiveTests(unittest.TestCase):
    """apply_link_exclusive：启用指定类型网卡、禁用另一类型。"""

    def test_wired_enables_wired_and_disables_wireless(self):
        adapters = {'802\\.3': [('以太网 2', 'Up')],
                    '802\\.11': [('WLAN', 'Up')]}
        ops = []
        with mock.patch('core.network._list_physical_adapters',
                        side_effect=lambda p: adapters.get(p, [])), \
             mock.patch('core.network._run_ps_adapter',
                        side_effect=lambda cmdlet, name, timeout=15: ops.append((cmdlet, name)) or True):
            ok, msg = apply_link_exclusive('wired')
        self.assertTrue(ok)
        # 先启用目标（有线），再禁用另一类型（无线）
        self.assertEqual(ops, [('Enable-NetAdapter', '以太网 2'),
                               ('Disable-NetAdapter', 'WLAN')])

    def test_missing_target_adapter_fails(self):
        with mock.patch('core.network._list_physical_adapters', return_value=[]):
            ok, msg = apply_link_exclusive('wired')
        self.assertFalse(ok)
        self.assertIn('未找到有线网卡', msg)

    def test_auto_is_noop(self):
        # auto 不动网卡（兼容旧行为）
        with mock.patch('core.network._run_ps_adapter') as run_ps:
            ok, _ = apply_link_exclusive('auto')
        self.assertTrue(ok)
        run_ps.assert_not_called()


if __name__ == '__main__':
    unittest.main()
