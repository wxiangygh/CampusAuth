"""WiFi 无线电自动开启（缺口一）：prepare_wifi_connection 回归测试。"""
from unittest import mock

import pytest

from core import network
from core.workflow import WorkflowContext, StepSpec


ZH_RADIO_ON = """    名称                   WLAN
    描述                   Intel(R) Wi-Fi 6 AX201
    无线电状态             硬件开/软件开
    状态                   已连接
"""

ZH_RADIO_SOFT_OFF = """    名称                   WLAN
    无线电状态             硬件开/软件关
    状态                   已断开连接
"""

EN_RADIO_SOFT_OFF = """    Name                   WiFi
    Radio state            Hardware On/Software Off
    State                  disconnected
"""

ZH_NO_INTERFACE = "系统上没有无线接口。\r\n"


def fake_run_command(outputs):
    """按调用顺序返回预设 (code, stdout, stderr)，命令文本包含关键词时匹配。"""
    queue = list(outputs)

    def _run(cmd, *args, **kwargs):
        if queue:
            return queue.pop(0)
        return 0, '', ''
    return _run


class TestRadioState:
    def test_zh_hardware_on_software_on(self):
        with mock.patch.object(network, 'run_command',
                               side_effect=fake_run_command([(0, ZH_RADIO_ON, '')])):
            assert network.get_wifi_radio_state() == ('on', 'on')

    def test_zh_software_off(self):
        with mock.patch.object(network, 'run_command',
                               side_effect=fake_run_command([(0, ZH_RADIO_SOFT_OFF, '')])):
            assert network.get_wifi_radio_state() == ('on', 'off')

    def test_en_software_off(self):
        with mock.patch.object(network, 'run_command',
                               side_effect=fake_run_command([(0, EN_RADIO_SOFT_OFF, '')])):
            assert network.get_wifi_radio_state() == ('on', 'off')

    def test_no_wireless_interface(self):
        with mock.patch.object(network, 'run_command',
                               side_effect=fake_run_command([(0, ZH_NO_INTERFACE, '')])):
            assert network.get_wifi_radio_state() == ('unknown', 'unknown')


class TestEnableRadio:
    def test_winrt_output_mapping(self):
        cases = [
            ('OK:ALREADY', True), ('OK:ON', True),
            ('ERR:NO_RADIO', False), ('ERR:DENIED_BY_USER', False),
            ('ERR:DENIED_BY_SYSTEM', False), ('ERR:FAILED_DeniedByUser', False),
            ('ERR:EXCEPTION', False),
        ]
        for output, expected_ok in cases:
            with mock.patch.object(network, 'run_command',
                                   side_effect=fake_run_command([(0, output + '\n', '')])):
                ok, msg = network._enable_wifi_radio_via_winrt()
            assert ok is expected_ok, (output, ok, msg)

    def test_script_fails(self):
        with mock.patch.object(network, 'run_command',
                               side_effect=fake_run_command([(1, '', 'boom')])):
            ok, msg = network._enable_wifi_radio_via_winrt()
        assert not ok
        assert '失败' in msg


@pytest.fixture
def no_sleep():
    with mock.patch.object(network.time, 'sleep'), \
            mock.patch('time.sleep'):
        yield


class TestPrepareWifiConnection:
    def test_ready_when_interface_and_radio_on(self, no_sleep):
        with mock.patch.object(network, 'get_wifi_interface_name', return_value='WLAN'), \
                mock.patch.object(network, 'get_wifi_radio_state', return_value=('on', 'on')), \
                mock.patch.object(network, '_enable_wifi_radio_via_winrt') as winrt, \
                mock.patch.object(network, '_list_physical_adapters') as adapters:
            ok, msg = network.prepare_wifi_connection()
        assert (ok, msg) == (True, '')
        winrt.assert_not_called()
        adapters.assert_not_called()

    def test_hardware_off_reports_clear_error(self, no_sleep):
        with mock.patch.object(network, 'get_wifi_interface_name', return_value='WLAN'), \
                mock.patch.object(network, 'get_wifi_radio_state', return_value=('off', 'on')), \
                mock.patch.object(network, '_enable_wifi_radio_via_winrt') as winrt:
            ok, msg = network.prepare_wifi_connection()
        assert not ok
        assert '硬件开关' in msg
        winrt.assert_not_called()

    def test_software_off_enables_via_winrt(self, no_sleep):
        states = iter([('on', 'off'), ('on', 'on')])
        with mock.patch.object(network, 'get_wifi_interface_name', return_value='WLAN'), \
                mock.patch.object(network, 'get_wifi_radio_state',
                                  side_effect=lambda: next(states)), \
                mock.patch.object(network, '_enable_wifi_radio_via_winrt',
                                  return_value=(True, 'WiFi 无线电已开启')) as winrt:
            ok, msg = network.prepare_wifi_connection()
        assert (ok, msg) == (True, '')
        winrt.assert_called_once()

    def test_software_off_winrt_failure_propagates(self, no_sleep):
        with mock.patch.object(network, 'get_wifi_interface_name', return_value='WLAN'), \
                mock.patch.object(network, 'get_wifi_radio_state',
                                  return_value=('on', 'off')), \
                mock.patch.object(network, '_enable_wifi_radio_via_winrt',
                                  return_value=(False, '系统拒绝控制无线电')):
            ok, msg = network.prepare_wifi_connection()
        assert not ok
        assert '拒绝' in msg

    def test_disabled_adapter_gets_enabled(self, no_sleep):
        interfaces = iter([None, 'WLAN'])
        with mock.patch.object(network, 'get_wifi_interface_name',
                               side_effect=lambda: next(interfaces)), \
                mock.patch.object(network, 'get_wifi_radio_state',
                                  return_value=('on', 'on')), \
                mock.patch.object(network, '_list_physical_adapters',
                                  return_value=[('WLAN', 'Disabled')]), \
                mock.patch.object(network, '_run_ps_adapter', return_value=True) as ps:
            ok, msg = network.prepare_wifi_connection()
        assert (ok, msg) == (True, '')
        ps.assert_called_once_with('Enable-NetAdapter', 'WLAN')

    def test_no_wireless_adapter(self, no_sleep):
        with mock.patch.object(network, 'get_wifi_interface_name', return_value=None), \
                mock.patch.object(network, '_list_physical_adapters', return_value=[]):
            ok, msg = network.prepare_wifi_connection()
        assert not ok
        assert '没有无线网卡' in msg


class TestConnectWifiIntegration:
    def test_connect_wifi_aborts_when_prepare_fails(self, no_sleep):
        with mock.patch.object(network, 'prepare_wifi_connection',
                               return_value=(False, 'WiFi 无线电硬件开关处于关闭状态')), \
                mock.patch.object(network, 'run_command') as run:
            ok, msg = network.connect_wifi('CMCC_TEST')
        assert not ok
        assert '硬件开关' in msg
        # netsh wlan connect 不应被调用
        assert not any('wlan' in str(call) and 'connect' in str(call)
                       for call in run.call_args_list)


class TestEnsureWifiWorkflow:
    def _context(self):
        return WorkflowContext(config={
            'wifi_name': 'CMCC_TEST', 'username': 'u', 'password': 'p',
        }, cancelled=lambda: False)

    def test_prepare_failure_returns_step_fail(self, no_sleep):
        from core import auth_workflow
        context = self._context()
        context.data['link_type'] = 'wireless'
        step = StepSpec(id='ensure_wifi', timeout=15)
        with mock.patch.object(auth_workflow, 'run_command',
                               side_effect=fake_run_command([(0, 'no interface', '')])), \
                mock.patch.object(auth_workflow, 'prepare_wifi_connection',
                                  return_value=(False, '系统拒绝控制无线电')):
            result = auth_workflow._ensure_wifi(context, step)
        assert not result.success
        assert result.code == 'wifi_prepare_failed'
        assert '拒绝' in result.message

    def test_prepare_skipped_when_already_connected(self, no_sleep):
        from core import auth_workflow
        context = self._context()
        context.data['link_type'] = 'wireless'
        step = StepSpec(id='ensure_wifi', timeout=15)
        output = f'    名称                   WLAN\n    SSID                  CMCC_TEST\n    状态                   已连接'
        with mock.patch.object(auth_workflow, 'run_command',
                               side_effect=fake_run_command([(0, output, '')])), \
                mock.patch.object(auth_workflow, 'prepare_wifi_connection') as prepare:
            result = auth_workflow._ensure_wifi(context, step)
        assert result.success
        prepare.assert_not_called()
