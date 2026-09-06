"""preempt_auth_lock：最新工作流请求打断在途操作的回归测试。"""
import threading
import time

import pytest

from core import state
from core.app_state import app_state
from core.auth import preempt_auth_lock


@pytest.fixture(autouse=True)
def clean_state():
    """隔离共享锁/取消标志/操作状态，避免用例互相影响。"""
    state._auth_lock.acquire()
    state._auth_lock.release()
    state._auth_cancelled.clear()
    yield
    state._auth_cancelled.clear()


class TestPreemptAuthLock:
    def test_lock_free_succeeds_without_cancelling(self):
        acquired, interrupted = preempt_auth_lock('测试请求')
        assert acquired is True
        assert interrupted is None
        assert not state._auth_cancelled.is_set()
        state._auth_lock.release()

    def test_interrupts_running_operation(self):
        def holder():
            state._auth_lock.acquire()
            app_state.start_operation('auth')
            app_state.update_operation(message='正在执行认证')
            # 模拟在途工作流：观察到取消标志后退出并释放锁
            while not state._auth_cancelled.wait(0.05):
                pass
            state._auth_lock.release()

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        time.sleep(0.2)
        acquired, interrupted = preempt_auth_lock('运行工作流「测试」', wait=2.0)
        assert acquired is True
        assert interrupted == '认证（正在执行认证）'
        # 取消标志已置位（新运行的 run_auth_workflow 会重新 clear）
        assert state._auth_cancelled.is_set()
        op = app_state.snapshot()['operation']
        assert op['status'] == 'cancelled'
        assert '已被新的请求取代' in op['message']
        # 抢占成功后锁归调用者所有（真实流程由 finally 释放）
        state._auth_lock.release()
        t.join(3)
        state._auth_cancelled.clear()

    def test_fails_when_holder_never_releases(self):
        state._auth_lock.acquire()
        app_state.start_operation('restore')
        try:
            acquired, interrupted = preempt_auth_lock('新请求', wait=0.2)
            assert acquired is False
            assert interrupted == '恢复网络'
            # 失败后取消标志应回滚，避免误杀后续无关探测
            assert not state._auth_cancelled.is_set()
        finally:
            state._auth_lock.release()

    def test_interrupt_desc_falls_back_to_kind(self):
        def holder():
            state._auth_lock.acquire()
            app_state.start_operation('restore')
            app_state.update_operation(message='')
            # 模拟在途工作流：观察到取消标志后退出并释放锁
            while not state._auth_cancelled.wait(0.05):
                pass
            state._auth_lock.release()

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        time.sleep(0.2)
        acquired, interrupted = preempt_auth_lock('新请求', wait=2.0)
        assert acquired is True
        assert interrupted == '恢复网络'
        state._auth_lock.release()
        t.join(3)
        state._auth_cancelled.clear()
