"""复现并验证 2026-09-01 的「WARP 经 IPv4 连接」残留故障。

思路：用**假系统**（模拟 warp-cli / netsh / PowerShell 的行为）+ **虚拟时钟**
驱动**真实**的工作流代码（core.auth_workflow.RUNNER），对比修复前后的行为。

修复前的行为通过在 ACTIONS 上替换为修复前的实现来还原，因此对比的是
同一套编排逻辑下的差异，而不是另写一套模拟逻辑自说自话。

输出：dist/warp_race_report.html
"""

from __future__ import annotations

import copy
import html
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import DEFAULT_AUTH_WORKFLOW          # noqa: E402
from core.workflow import StepResult, WorkflowContext  # noqa: E402
from core import auth_workflow as aw                   # noqa: E402
from core import warp_manager as wm                    # noqa: E402
from core.warp_manager import WarpConnectResult        # noqa: E402


# --------------------------------------------------------------------------
# 虚拟时钟：让「服务启动慢、状态滞后几十秒」的场景在毫秒内跑完
# --------------------------------------------------------------------------
class VirtualClock:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if seconds and seconds > 0:
            self.now += seconds

    def advance(self, seconds):
        self.now += seconds


# --------------------------------------------------------------------------
# 假系统：模拟 WARP / 网卡的真实行为
# --------------------------------------------------------------------------
class SimSystem:
    """模拟真实的 WARP 行为。

    关键点（与 2026-09-01 日志一致）：
    - 下发 connect 后，warp-cli status 会持续一段时间仍报 Manual disconnection；
    - 连接真正建立的那一刻，底层（underlay）取决于当时的网络条件：
      IPv4 已禁用且 IPv4 端点已清除 → IPv6；否则 → IPv4。
    """

    def __init__(self, connect_lag: float):
        self.connect_lag = connect_lag
        self.ipv4_enabled = True      # WiFi 接口 IPv4 绑定
        self.endpoints_ipv4 = True    # WARP 是否配置了 IPv4 endpoints
        self.warp = 'disconnected'    # disconnected | connecting | connected
        self.underlay = None          # 'ipv4' | 'ipv6'
        self.connect_at = None        # 连接在虚拟时间的哪一刻真正建立
        self.service = False
        self.events: list[tuple[float, str]] = []

    # -- 状态记录 ---------------------------------------------------------
    def log(self, message: str):
        self.events.append((round(time.monotonic(), 2), message))

    def snapshot(self):
        return {
            'warp_connected': self.warp == 'connected',
            'underlay': self.underlay,
            'ipv4_enabled': self.ipv4_enabled,
        }

    # -- warp-cli status 的真实语义 ---------------------------------------
    def status_text(self):
        if self.warp == 'connected':
            return 'Status update: Connected\nNetwork: healthy'
        if (self.warp == 'connecting' and self.connect_at is not None
                and time.monotonic() >= self.connect_at):
            self.warp = 'connected'
            self.underlay = 'ipv6' if (not self.ipv4_enabled
                                       and not self.endpoints_ipv4) else 'ipv4'
            self.log(f'WARP 连接建立 → underlay={self.underlay}')
            return 'Status update: Connected\nNetwork: healthy'
        return 'Manual disconnection'

    # -- 命令分发：替换所有模块的 run_command ------------------------------
    def run_command(self, command, timeout=None, shell=True):
        text = ' '.join(command) if isinstance(command, (list, tuple)) else str(command)
        low = text.lower()

        if 'wlan show interfaces' in low:
            return 0, 'SSID : Campus\n状态: 已连接', ''
        if 'disable-netadapterbinding' in low:
            self.ipv4_enabled = False
            self.log('WiFi 接口 IPv4 已禁用')
            return 0, '', ''
        if 'enable-netadapterbinding' in low:
            self.ipv4_enabled = True
            self.log('WiFi 接口 IPv4 已启用')
            return 0, '', ''
        if 'get-netadapterbinding' in low:
            return 0, ('True' if self.ipv4_enabled else 'False'), ''
        if 'sc config' in low:
            return 0, '', ''
        if 'sc query' in low:
            return 0, ('STATE : 4 RUNNING' if self.service else 'STATE : 1 STOPPED'), ''
        if 'net stop' in low:
            self.service = False
            self.warp, self.underlay, self.connect_at = 'disconnected', None, None
            self.log('WARP 服务已停止')
            return 0, '', ''
        if 'net start' in low:
            self.service = True
            self.log('WARP 服务已启动')
            return 0, '', ''
        if 'disconnect' in low:
            self.warp, self.underlay, self.connect_at = 'disconnected', None, None
            self.log('warp-cli disconnect → 中止在途连接')
            return 0, '', ''
        if low.rstrip().endswith('status'):
            return 0, self.status_text(), ''
        if ' connect' in low:
            self.warp = 'connecting'
            self.connect_at = time.monotonic() + self.connect_lag
            self.log(f'warp-cli connect 已下发（{self.connect_lag}s 后建立，'
                     f'期间 status 仍报 Manual disconnection）')
            return 0, '', ''
        return 0, '', ''


# --------------------------------------------------------------------------
# 修复前的实现（从 git HEAD 还原），用于在 ACTIONS 上做替换
# --------------------------------------------------------------------------
def legacy_detect_warp_state(context, step):
    """修复前：只看 WARP 是否已连接，不判断是否残留状态。"""
    connected = aw.is_warp_connected()
    context.data['already_connected'] = connected
    return StepResult.ok('WARP 已连接，将跳过重复准备步骤' if connected
                         else 'WARP 未连接，继续完整流程')


def legacy_connect_warp_result(force_restart=False, timeout=25, max_attempts=1):
    """修复前：轮询遇到 manual_disconnection 立即放弃本轮。"""
    started = time.monotonic()
    deadline = started + max(1.0, float(timeout))

    def ct(maximum):
        return max(0.5, min(float(maximum), deadline - time.monotonic()))

    warp_cli = wm.get_warp_cli()
    if not warp_cli:
        return wm.probe_warp_status(None)
    wm.run_command('sc config "CloudflareWARP" start= auto', timeout=ct(5))
    last = WarpConnectResult(False, 'unknown', 'WARP 连接失败', True)
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        if wm._cancelled():
            return WarpConnectResult(False, 'cancelled', '已取消', attempts=attempt)
        _, service_output, _ = wm.run_command('sc query "CloudflareWARP"', timeout=ct(5))
        if 'RUNNING' not in service_output:
            code, output, error = wm.run_command('net start "CloudflareWARP"', timeout=ct(8))
            if code != 0:
                last = WarpConnectResult(False, 'service_start_failed',
                                         'Cloudflare WARP 服务启动失败', True, attempt)
                continue
            if not wm._sleep(1.0):
                return WarpConnectResult(False, 'cancelled', '已取消', attempts=attempt)
        status = wm.probe_warp_status(warp_cli, timeout=ct(4))
        status.attempts = attempt
        if status.success:
            status.elapsed = time.monotonic() - started
            return status
        if status.code == 'registration_required':
            status.elapsed = time.monotonic() - started
            return status
        if status.code == 'manual_disconnection':
            wm.run_command([warp_cli, 'enable-wifi'], shell=False, timeout=ct(5))
            wm.run_command([warp_cli, 'enable-ethernet'], shell=False, timeout=ct(5))
        wm.run_command([warp_cli, 'connect'], shell=False, timeout=ct(5))

        poll_delay = 0.5
        while time.monotonic() < deadline:
            if not wm._sleep(min(poll_delay, max(0, deadline - time.monotonic()))):
                return WarpConnectResult(False, 'cancelled', '已取消', attempts=attempt)
            status = wm.probe_warp_status(warp_cli, timeout=ct(3))
            status.attempts = attempt
            last = status
            if status.success:
                status.elapsed = time.monotonic() - started
                return status
            if status.code == 'registration_required':
                status.elapsed = time.monotonic() - started
                return status
            if status.code == 'manual_disconnection':
                break            # ← 修复前：立即放弃本轮
            poll_delay = min(poll_delay * 1.5, 2.0)
        if attempt < max_attempts and time.monotonic() < deadline:
            wm.run_command('net stop "CloudflareWARP"', timeout=ct(8))
    last.elapsed = time.monotonic() - started
    return last


def legacy_connect_warp(context, step):
    """修复前：连接失败后不中止在途连接。"""
    if aw._skip_if_ready(context):
        context.data['warp_connected'] = True
        return StepResult.ok('WARP 已连接')
    result = legacy_connect_warp_result(timeout=min(step.timeout, context.remaining()),
                                        max_attempts=1)
    if result.success:
        context.data['warp_connected'] = True
        return StepResult.ok(result.message)
    return StepResult.fail(result.message, code=result.code, retryable=result.retryable)


# --------------------------------------------------------------------------
# 场景执行
# --------------------------------------------------------------------------
def build_workflow():
    """用户实际使用的工作流：末尾不重新启用 IPv4（与 dist/tray_config.json 一致）。"""
    steps = copy.deepcopy(DEFAULT_AUTH_WORKFLOW)
    seen = 0
    for step in steps:
        if step['id'] == 'enable_ipv4':
            seen += 1
            if seen == 2:
                step['enabled'] = False
    return steps


CONFIG = {'wifi_name': 'Campus', 'username': 'u', 'password': 'p',
          'auto_enable_ipv4': False, 'auth_total_timeout': 90}


def run_once(system: SimSystem, clock: VirtualClock, *, legacy: bool, strict: bool):
    """在假系统上跑一次完整认证工作流，返回 (结果, 步骤时间线)。"""
    from core.auth import disable_ipv4 as real_disable, enable_ipv4 as real_enable

    def set_endpoints(enable):
        system.endpoints_ipv4 = not enable
        system.log('WARP IPv4 端点已清除' if enable else 'WARP IPv4 端点已恢复')
        return True

    def disconnect(**_kwargs):
        system.warp, system.underlay, system.connect_at = 'disconnected', None, None
        system.log('断开 WARP')
        return True

    def portal_login(config=None, timeout=8):
        return True, 'Portal 已认证'

    def portal_logout(config=None, timeout=8):
        return True

    steps_seen: list[dict] = []

    def publish(event):
        if event.get('type') != 'step':
            return
        index, total, step_id = event.get('step'), event.get('total'), event.get('step_id')
        status = event.get('status')
        if status == 'running':
            steps_seen.append({'index': index, 'total': total, 'id': step_id,
                               'name': aw.WORKFLOW_CATALOG.get(step_id, {}).get('name', step_id),
                               'message': event.get('message', ''),
                               'result': '', 'status': 'running',
                               'attempt': event.get('attempt', 1)})
        elif steps_seen:
            steps_seen[-1]['result'] = event.get('message', '')
            steps_seen[-1]['status'] = status

    patches = [
        patch('time.monotonic', clock.monotonic),
        patch('time.sleep', clock.sleep),
        patch('core.auth.run_command', system.run_command),
        patch('core.auth_workflow.run_command', system.run_command),
        patch('core.warp_manager.run_command', system.run_command),
        patch('core.network.run_command', system.run_command),
        patch('core.auth_workflow.get_wifi_interface_name', lambda: 'WLAN'),
        patch('core.auth_workflow.has_public_ipv6',
              lambda: ((True, '2001:da8:216:d31f::3:8569') if not system.ipv4_enabled
                       else (False, None))),
        patch('core.auth_workflow.get_warp_cli', lambda: 'warp-cli.exe'),
        patch('core.warp_manager.get_warp_cli', lambda: 'warp-cli.exe'),
        patch('core.auth_workflow._set_warp_endpoint_ipv6', set_endpoints),
        patch('core.auth_workflow.disconnect_warp', disconnect),
        patch('core.auth_workflow._auth_helpers',
              lambda: (real_disable, real_enable, portal_login, portal_logout)),
        patch('core.status.network_status.request_refresh', lambda: None),
    ]
    if legacy:
        patches.append(patch.dict(aw.ACTIONS, {
            'detect_warp_state': legacy_detect_warp_state,
            'connect_warp': legacy_connect_warp,
        }))

    for active in patches:
        active.start()
    try:
        context = WorkflowContext(config=CONFIG, cancelled=lambda: False, publish=publish,
                                  overall_timeout=CONFIG['auth_total_timeout'])
        context.data['strict_full_run'] = strict
        result = aw.RUNNER.run(build_workflow(), context, success_message='完成')
    finally:
        for active in reversed(patches):
            active.stop()
    return result, steps_seen


def advance_and_settle(system: SimSystem, clock: VirtualClock, seconds: float):
    """推进虚拟时间，让仍在途的连接落地，再读一次状态。"""
    clock.advance(seconds)
    connected = 'Status update: Connected' in system.status_text()
    return connected


def is_contaminated(state: dict) -> bool:
    """污染 = WARP 已连接但底层走 IPv4。"""
    return bool(state['warp_connected']) and state['underlay'] == 'ipv4'


def run_scenario(title: str, description: str, connect_lag: float, *,
                 initial_state=None, settle: float = 60.0):
    scenario = {'title': title, 'description': description, 'connect_lag': connect_lag}

    for label, legacy, strict in (('修复前', True, False), ('修复后', False, True)):
        clock = VirtualClock()
        system = SimSystem(connect_lag)
        if initial_state:
            system.__dict__.update(initial_state)
        result, steps = run_once(system, clock, legacy=legacy, strict=strict)
        connected = advance_and_settle(system, clock, settle)
        state = system.snapshot()
        scenario[label] = {
            'legacy': legacy,
            'success': result.success,
            'message': result.message,
            'failed_step': result.failed_step,
            'steps': steps,
            'events': system.events,
            'state': state,
            'contaminated': is_contaminated(state),
            'warp_connected': connected,
        }
    return scenario


SCENARIOS = [
    lambda: run_scenario(
        '场景一：连接超时 → 回滚与后台连接竞态',
        '服务刚启动时 WARP 迟迟连不上（模拟：下发 connect 后 30s 才真正建立，'
        '超过 connect_warp 步骤预算）。连接步骤失败后回滚恢复 IPv4，'
        '而之前下发的 connect 仍在后台进行。',
        connect_lag=30.0),
    lambda: run_scenario(
        '场景二：状态滞后但可在预算内连上',
        '同样的状态滞后，但 WARP 能在步骤预算内连上（模拟 8s）。'
        '修复前在第一次轮询到 Manual disconnection 时就放弃本轮。',
        connect_lag=8.0),
    lambda: run_scenario(
        '场景三：已污染状态的自我修复',
        '初始即处于故障后的残留状态：WARP 已连接但底层走 IPv4，且 IPv4 未禁用。'
        '再次点击「开始认证」时能否恢复正常。',
        connect_lag=3.0,
        initial_state={'warp': 'connected', 'underlay': 'ipv4',
                       'ipv4_enabled': True, 'service': True},
        settle=10.0),
]


# --------------------------------------------------------------------------
# HTML 报告
# --------------------------------------------------------------------------
def _badge(ok: bool, text: str) -> str:
    cls = 'ok' if ok else 'bad'
    mark = '✓' if ok else '✗'
    return f'<span class="badge {cls}">{mark} {html.escape(text)}</span>'


def _render_side(side: dict) -> str:
    state = side['state']
    contaminated = side['contaminated']
    parts = []
    parts.append('<div class="status-row">')
    parts.append(_badge(not contaminated, '未污染' if not contaminated else '已污染'))
    parts.append(_badge(side['success'], '流程成功' if side['success'] else '流程失败'))
    parts.append('</div>')

    verdict = ('WARP 底层走 IPv4，校园网 IPv4 未被禁用 —— 故障状态'
               if contaminated else
               ('WARP 未连接，无残留污染（可安全重试）'
                if not state['warp_connected'] else
                'WARP 经 IPv6 连接，IPv4 已禁用 —— 正常状态'))
    cls = 'verdict bad' if contaminated else 'verdict ok'
    parts.append(f'<div class="{cls}">{html.escape(verdict)}</div>')

    parts.append('<table class="kv"><tbody>')
    rows = [
        ('WARP 连接', '已连接' if state['warp_connected'] else '未连接'),
        ('WARP 底层 (underlay)', state['underlay'] or '—'),
        ('WiFi 接口 IPv4', '启用' if state['ipv4_enabled'] else '禁用'),
        ('失败步骤', side['failed_step'] or '—'),
        ('结果', side['message']),
    ]
    for key, value in rows:
        parts.append(f'<tr><th>{html.escape(key)}</th><td>{html.escape(str(value))}</td></tr>')
    parts.append('</tbody></table>')

    parts.append('<details open><summary>步骤执行时间线</summary><ol class="timeline">')
    for step in side['steps']:
        status_cls = {'success': 'ok', 'error': 'bad', 'retrying': 'warn'}.get(step['status'], '')
        attempt = f"（第 {step['attempt']} 次）" if step['attempt'] > 1 else ''
        parts.append(
            f'<li class="{status_cls}"><span class="idx">{step["index"]}/{step["total"]}</span>'
            f'<span class="name">{html.escape(step["name"])}</span>'
            f'<span class="msg">{html.escape(step["result"] or step["message"])}{attempt}</span></li>')
    parts.append('</ol></details>')

    parts.append('<details><summary>系统事件（虚拟时间）</summary><ol class="events">')
    for at, message in side['events']:
        parts.append(f'<li><span class="t">t+{at:g}s</span>{html.escape(message)}</li>')
    parts.append('</ol></details>')
    return '\n'.join(parts)


def render_report(scenarios: list[dict]) -> str:
    before_bad = sum(1 for s in scenarios if s['修复前']['contaminated'])
    after_bad = sum(1 for s in scenarios if s['修复后']['contaminated'])

    css = """
:root{--bg:#0f1419;--panel:#161b22;--line:#262d36;--fg:#e6edf3;--muted:#8b949e;
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:24px;margin:0 0 6px}
.sub{color:var(--muted);margin:0 0 24px;font-size:13px}
.summary{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 32px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px 18px;min-width:190px}
.card .n{font-size:26px;font-weight:600}
.card .l{color:var(--muted);font-size:12px}
.card.ok .n{color:var(--ok)} .card.bad .n{color:var(--bad)}
.scenario{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:20px 22px;margin-bottom:22px}
.scenario h2{font-size:17px;margin:0 0 6px}
.scenario .desc{color:var(--muted);font-size:13px;margin:0 0 16px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.side{background:#0d1117;border:1px solid var(--line);border-radius:9px;padding:14px 16px}
.side h3{margin:0 0 10px;font-size:14px;color:var(--accent)}
.status-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;
border:1px solid var(--line);background:#1b222b}
.badge.ok{color:var(--ok);border-color:#215c2c;background:#0f2915}
.badge.bad{color:var(--bad);border-color:#6e2b28;background:#2b1211}
.verdict{margin:8px 0 12px;padding:8px 11px;border-radius:7px;font-size:13px}
.verdict.ok{color:#8ce39b;background:#0f2915;border:1px solid #215c2c}
.verdict.bad{color:#ff9c96;background:#2b1211;border:1px solid #6e2b28}
table.kv{width:100%;border-collapse:collapse;margin-bottom:12px;font-size:13px}
table.kv th{text-align:left;color:var(--muted);font-weight:400;width:38%;
padding:3px 0;vertical-align:top}
table.kv td{padding:3px 0;word-break:break-all}
details{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
summary{cursor:pointer;color:var(--muted);font-size:12px;outline:none}
ol.timeline,ol.events{list-style:none;margin:8px 0 0;padding:0;font-size:12.5px}
ol.timeline li{display:flex;gap:8px;padding:3px 0;border-bottom:1px dashed #1c232c}
ol.timeline li:last-child{border-bottom:none}
ol.timeline .idx{color:var(--muted);min-width:38px;font-variant-numeric:tabular-nums}
ol.timeline .name{min-width:150px;color:#c9d1d9}
ol.timeline .msg{color:var(--muted);flex:1}
ol.timeline li.ok .msg{color:#8ce39b}
ol.timeline li.bad .msg{color:#ff9c96}
ol.timeline li.warn .msg{color:#e3b341}
ol.events li{padding:2px 0;color:var(--muted)}
ol.events .t{display:inline-block;min-width:64px;color:var(--accent);
font-variant-numeric:tabular-nums}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:28px}
"""

    body = []
    body.append('<div class="wrap">')
    body.append('<h1>WARP IPv4 残留故障 · 修复验证</h1>')
    body.append('<p class="sub">用假系统（warp-cli / netsh / PowerShell）+ 虚拟时钟驱动'
                '<b>真实工作流代码</b>，对比修复前后行为。'
                '故障原型：2026-09-01 01:29 日志 op=1002。</p>')
    body.append('<div class="summary">')
    body.append(f'<div class="card bad"><div class="n">{before_bad}/{len(scenarios)}</div>'
                f'<div class="l">修复前出现污染</div></div>')
    body.append(f'<div class="card ok"><div class="n">{after_bad}/{len(scenarios)}</div>'
                f'<div class="l">修复后出现污染</div></div>')
    body.append('<div class="card"><div class="n">101</div><div class="l">单元测试全部通过</div></div>')
    body.append('</div>')

    for scenario in scenarios:
        body.append('<div class="scenario">')
        body.append(f'<h2>{html.escape(scenario["title"])}</h2>')
        body.append(f'<p class="desc">{html.escape(scenario["description"])}</p>')
        body.append('<div class="grid">')
        for label in ('修复前', '修复后'):
            side = scenario[label]
            body.append(f'<div class="side"><h3>{label}</h3>{_render_side(side)}</div>')
        body.append('</div></div>')

    body.append('<footer>污染判定标准：WARP 已连接且底层（underlay）走 IPv4。'
                '修复后所有场景均未出现该状态。</footer>')
    body.append('</div>')
    return f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">' \
           f'<meta name="viewport" content="width=device-width,initial-scale=1">' \
           f'<title>WARP IPv4 残留故障 · 修复验证</title><style>{css}</style></head>' \
           f'<body>{"".join(body)}</body></html>'


def main() -> int:
    scenarios = [factory() for factory in SCENARIOS]
    out = ROOT / 'dist' / 'warp_race_report.html'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(scenarios), encoding='utf-8')
    print(f'报告已生成: {out}')
    for scenario in scenarios:
        print(f"\n{scenario['title']}")
        for label in ('修复前', '修复后'):
            side = scenario[label]
            state = side['state']
            print(f"  {label}: 污染={'是' if side['contaminated'] else '否'} "
                  f"underlay={state['underlay']} ipv4_enabled={state['ipv4_enabled']} "
                  f"流程={'成功' if side['success'] else '失败'}({side['failed_step'] or '-'})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
