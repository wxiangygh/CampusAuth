"""把定向解析规则下发到 Windows NRPT（名称解析策略表）。

NRPT 是 Windows 自带的「按命名空间选 DNS 服务器」机制：命中的域名后缀 / IP / 网段
由规则里的 NameServers 解析，其余查询仍走网卡 DNS（WARP 接管时即 127.0.0.2）。
因此下发后浏览器、命令行进程等所有解析方都会按绑定规则走指定服务器。

读取策略表普通权限即可，写入需要管理员：脚本落临时文件由 ShellExecuteW 提权执行，
结果用 JSON 文件回传（提权进程没有可捕获的 stdout）。
"""
import ctypes
import json
import logging
import os
import subprocess
import tempfile
import time
from ipaddress import ip_address, ip_network

from core.command import run_command_simple, run_powershell_simple

logger = logging.getLogger('wifi_tray')


def ps_quote(value):
    """把任意值包成 PowerShell 单引号字面量（单引号翻倍转义）。"""
    return "'" + str(value).replace("'", "''") + "'"


def nrpt_namespace(target):
    """绑定目标 → NRPT 命名空间写法。

    域名前置点：表示该域及其所有子域名；
    IPv4 网段用子网掩码形式（NRPT 不认 CIDR 长度），/32 直接写地址；
    IPv6 保留前缀长度写法。
    """
    text = str(target).strip().lower()
    try:
        net = ip_network(text, strict=False)
    except ValueError:
        return '.' + text
    if net.version == 6:
        return text if net.prefixlen == 128 else str(net)
    if net.prefixlen == 32:
        return str(net.network_address)
    return f'{net.network_address}/{ip_address(net.netmask)}'


def list_rules():
    """读取本机 NRPT 规则，返回 [{'name','namespaces','servers'}]。"""
    script = (
        '$r = @(Get-DnsClientNrptRule -EA SilentlyContinue | '
        'Select-Object Name,'
        # 这里不能用 ",@(...) -join"：前置逗号构造的数组会被 Select-Object 先字符串化，
        # 读回来就成了字面量 System.String[]（2026-09-26 实测）
        '@{n="Namespaces";e={(@($_.Namespace)) -join ";"}},'
        '@{n="Servers";e={(@($_.NameServers)) -join ";"}}); '
        'if ($r) { ConvertTo-Json -InputObject $r -Compress }'
    )
    code, out, _ = run_powershell_simple(script, timeout=20)
    if code != 0 or not out.strip():
        return []
    try:
        data = json.loads(out.strip())
    except ValueError:
        logger.warning('NRPT rule list is not valid JSON: %s', out[:200])
        return []
    items = data if isinstance(data, list) else [data]
    return [{'name': str(item.get('Name') or ''),
             'namespaces': [x for x in str(item.get('Namespaces') or '').split(';') if x],
             'servers': [x for x in str(item.get('Servers') or '').split(';') if x]}
            for item in items]


def _run_elevated(body, timeout=90):
    """提权执行 PowerShell 片段，返回其写入 $payload 的 dict。

    约定 body 把结果赋给 $payload；异常由包装层捕获成 {ok:$false,error=...}。
    """
    tag = f'{os.getpid()}_{int(time.time() * 1000)}'
    result_file = os.path.join(tempfile.gettempdir(), f'cauth_nrpt_{tag}.json')
    done_file = os.path.join(tempfile.gettempdir(), f'cauth_nrpt_done_{tag}.txt')
    script_file = os.path.join(tempfile.gettempdir(), f'cauth_nrpt_{tag}.ps1')
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$result = {ps_quote(result_file)}\n"
        "$payload = $null\n"
        "try {\n"
        f"{body}\n"
        "} catch {\n"
        "  $payload = [pscustomobject]@{ok=$false; error=$_.Exception.Message; names=@()}\n"
        "}\n"
        "ConvertTo-Json -InputObject $payload -Compress -Depth 5 |"
        " Out-File -FilePath $result -Encoding utf8\n"
        f"1 | Out-File -FilePath {ps_quote(done_file)} -Encoding ascii\n"
    )
    try:
        # utf-8-sig：PowerShell 5.1 需要 BOM 才按 UTF-8 读脚本，否则中文注释会解析失败
        with open(script_file, 'w', encoding='utf-8-sig') as f:
            f.write(script)
    except OSError as exc:
        return {'ok': False, 'error': f'写入提权脚本失败：{exc}'}

    params = f'-NoProfile -ExecutionPolicy Bypass -File "{script_file}"'
    ret = ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'powershell.exe', params, None, 0)
    if ret <= 32:  # ShellExecute 的成功返回值是 >32 的实例句柄
        logger.warning('NRPT elevate failed: ShellExecuteW=%s', ret)
        return {'ok': False, 'error': '未获得管理员权限（UAC 被取消或无法启动 PowerShell）'}

    deadline = time.time() + timeout
    while time.time() < deadline and not os.path.exists(done_file):
        time.sleep(0.3)
    payload = {'ok': False, 'error': '提权脚本超时未返回', 'names': []}
    if os.path.exists(done_file):
        time.sleep(0.2)
        try:
            with open(result_file, 'r', encoding='utf-8-sig') as f:
                payload = json.loads(f.read().strip() or '{}')
        except (OSError, ValueError) as exc:
            payload['error'] = f'读取提权结果失败：{exc}'
    for path in (script_file, result_file, done_file):
        try:
            os.remove(path)
        except OSError:
            pass
    return payload


def _sync_script(desired, stale_names, stale_namespaces):
    """生成同步脚本：先清掉本应用写入的旧规则，再按当前绑定重新添加。"""
    lines = ['$names = @()']
    for name in stale_names:
        lines.append(f'Remove-DnsClientNrptRule -Name {ps_quote(name)} -Force -EA SilentlyContinue')
    if stale_namespaces:
        quoted = ','.join(ps_quote(ns) for ns in stale_namespaces)
        # 显式遍历：嵌套 Where-Object 里 $_ 会被内层管道覆盖，匹配不到命名空间
        lines.append(
            '$stale = @(' + quoted + ')\n'
            'foreach ($rule in @(Get-DnsClientNrptRule -EA SilentlyContinue)) {\n'
            '  foreach ($ns in $rule.Namespace) {\n'
            '    if ($stale -contains $ns) {\n'
            '      Remove-DnsClientNrptRule -Name $rule.Name -Force -EA SilentlyContinue\n'
            '      break\n'
            '    }\n'
            '  }\n'
            '}\n'
        )
    for rule in desired:
        servers = ','.join(ps_quote(s) for s in rule['servers'])
        # Add-DnsClientNrptRule 没有 -Force（只有 Remove- 有），带上会让整条
        # 提权脚本在 $ErrorActionPreference=Stop 下抛错，一条都写不进去。
        lines.append(
            f'$r = Add-DnsClientNrptRule -Namespace {ps_quote(rule["namespace"])}'
            f' -NameServers {servers} -PassThru\n'
            'if ($r) { $names += $r.Name }'
        )
    lines.append('$payload = [pscustomobject]@{ok=$true; names=$names}')
    return '\n'.join(lines)


def flush_cache():
    """清空系统 DNS 缓存，让新策略立即生效。"""
    run_command_simple('ipconfig /flushdns', shell=True, timeout=15)


def sync(bindings, state=None):
    """把启用中的绑定规则下发到 NRPT。返回 (ok, message, new_state)。

    state 是上次下发的记录（规则名与命名空间），用于精确清理，
    即使配置被手改也能按命名空间兜底扫掉残留。
    """
    state = state or {}
    desired = [{'target': r['target'],
                'namespace': nrpt_namespace(r['target']),
                'servers': r['servers']}
               for r in bindings if r.get('enabled', True)]
    stale_names = list(state.get('names') or [])
    stale_namespaces = sorted({r['namespace'] for r in desired}
                              | set(state.get('namespaces') or []))
    payload = _run_elevated(_sync_script(desired, stale_names, stale_namespaces))
    if not payload.get('ok'):
        return False, f'NRPT 下发失败：{payload.get("error") or "未知错误"}', state

    names = [str(n) for n in (payload.get('names') or []) if n]
    if len(names) != len(desired):
        logger.warning('NRPT applied %s of %s rules', len(names), len(desired))
    new_state = {'names': names,
                 'namespaces': sorted({r['namespace'] for r in desired}),
                 'targets': [r['target'] for r in desired],
                 'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    flush_cache()
    if not desired:
        return True, '已清除系统级解析规则', new_state
    tracked = '' if len(names) == len(desired) else \
        f'（已记录 {len(names)}/{len(desired)} 条，未记录的按命名空间清理）'
    return True, f'已下发 {len(desired)} 条规则到系统（NRPT）{tracked}', new_state


def clear(state=None):
    """移除本应用写入的全部 NRPT 规则。返回 (ok, message, new_state)。"""
    state = state or {}
    payload = _run_elevated(_sync_script([], state.get('names') or [],
                                        state.get('namespaces') or []))
    if not payload.get('ok'):
        return False, f'NRPT 清除失败：{payload.get("error") or "未知错误"}', state
    flush_cache()
    return True, '已清除系统级解析规则', {'names': [], 'namespaces': [], 'targets': [],
                                          'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')}


def overview(bindings, state=None):
    """DNS 设置页概览：本机 NRPT 现状与本应用已下发的规则。"""
    state = state or {}
    live = list_rules()
    applied = [ns for ns in (state.get('namespaces') or []) if ns]
    matched = [r for r in live if set(r['namespaces']) & set(applied)]
    return {
        'enabled_bindings': len([b for b in bindings if b.get('enabled', True)]),
        'applied': len(matched),
        'applied_targets': state.get('targets') or [],
        'system_rules': len(live),
        'updated_at': state.get('updated_at') or '',
    }
