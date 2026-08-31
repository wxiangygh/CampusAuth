"""Configurable, observable workflow runner used by authentication."""
from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("wifi_tray")


@dataclasses.dataclass(frozen=True)
class StepSpec:
    id: str
    enabled: bool = True
    retries: int = 0
    timeout: float = 15.0
    retry_delay: float = 1.0
    continue_on_error: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StepSpec":
        return cls(id=str(value.get("id", "")).strip(),
                   enabled=bool(value.get("enabled", True)),
                   retries=max(0, min(int(value.get("retries", 0)), 5)),
                   timeout=max(1.0, min(float(value.get("timeout", 15)), 180.0)),
                   retry_delay=max(0.0, min(float(value.get("retry_delay", 1)), 30.0)),
                   continue_on_error=bool(value.get("continue_on_error", False)))


@dataclasses.dataclass
class StepResult:
    success: bool
    message: str
    code: str = "ok"
    retryable: bool = False
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def ok(cls, message: str, **details: Any) -> "StepResult":
        return cls(True, message, details=details)

    @classmethod
    def fail(cls, message: str, *, code: str = "failed", retryable: bool = False,
             **details: Any) -> "StepResult":
        return cls(False, message, code=code, retryable=retryable, details=details)


@dataclasses.dataclass
class WorkflowResult:
    success: bool
    message: str
    code: str
    failed_step: str | None = None
    elapsed: float = 0.0
    completed_steps: tuple[str, ...] = ()
    # 每个执行过的节点的运行数据：{step_id: {'elapsed': 各次尝试耗时之和,
    # 'retries': 实际重试次数, 'success': 是否成功, 'timeout': 该节点超时配置,
    # 'executed': 是否真实执行（取消/总时限中止时，未执行的节点为占位记录）}}
    # 供运行结束后记录统计、自动调优使用
    step_stats: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)


class WorkflowContext:
    def __init__(self, *, config: dict[str, Any], cancelled: Callable[[], bool],
                 publish: Callable[[dict[str, Any]], None] | None = None,
                 overall_timeout: float | None = None):
        self.config = config
        self.cancelled = cancelled
        self.publish = publish or (lambda event: None)
        self.data: dict[str, Any] = {}
        self.current_attempt = 1
        self.current_step: StepSpec | None = None
        self.deadline: float | None = None
        self.overall_deadline = (time.monotonic() + max(1.0, float(overall_timeout))
                                 if overall_timeout is not None else None)
        self._rollbacks: list[tuple[str, Callable[[], None]]] = []

    def remaining(self, fallback: float = 1.0) -> float:
        deadlines = [value for value in (self.deadline, self.overall_deadline) if value is not None]
        return fallback if not deadlines else max(0.1, min(deadlines) - time.monotonic())

    def add_rollback(self, name: str, callback: Callable[[], None]) -> None:
        self.remove_rollback(name)
        self._rollbacks.append((name, callback))

    def remove_rollback(self, name: str) -> None:
        self._rollbacks = [(item_name, callback) for item_name, callback in self._rollbacks
                           if item_name != name]

    def rollback(self) -> None:
        while self._rollbacks:
            name, callback = self._rollbacks.pop()
            try:
                callback()
            except Exception:
                logger.exception("Workflow rollback failed: %s", name)

    def clear_rollbacks(self) -> None:
        self._rollbacks.clear()


class WorkflowRunner:
    def __init__(self, actions: dict[str, Callable[[WorkflowContext, StepSpec], StepResult]],
                 catalog: dict[str, dict[str, Any]]):
        self.actions = actions
        self.catalog = catalog

    def validate(self, raw_steps: list[dict[str, Any]]) -> list[StepSpec]:
        if not isinstance(raw_steps, list):
            raise ValueError("工作流必须是步骤列表")
        steps = [StepSpec.from_dict(value) for value in raw_steps]
        if not steps:
            raise ValueError("工作流不能为空")
        for step in steps:
            if not step.id or step.id not in self.actions:
                raise ValueError(f"未知工作流步骤: {step.id or '<empty>'}")
        enabled = [step for step in steps if step.enabled]
        if not enabled:
            raise ValueError("至少需要启用一个工作流步骤")
        return steps

    def run(self, raw_steps: list[dict[str, Any]], context: WorkflowContext,
            success_message: str = "工作流执行完成") -> WorkflowResult:
        steps = [step for step in self.validate(raw_steps) if step.enabled]
        started = time.monotonic()
        completed: list[str] = []
        stats: dict[str, dict[str, Any]] = {}
        total = len(steps)
        for index, step in enumerate(steps, 1):
            # 尚未执行就被取消/超过总时限的节点也留占位记录（executed: False），
            # 保证每次运行的结果都能完整反映各节点状态；调优侧会跳过这类样本
            if context.overall_deadline is not None and time.monotonic() >= context.overall_deadline:
                stats.setdefault(step.id, {'elapsed': 0.0, 'retries': 0, 'success': False,
                                           'timeout': step.timeout, 'executed': False})
                context.rollback()
                return WorkflowResult(False, '工作流超过总时限，已执行回滚',
                                      'workflow_timeout', step.id,
                                      time.monotonic() - started, tuple(completed),
                                      step_stats=stats)
            if context.cancelled():
                stats.setdefault(step.id, {'elapsed': 0.0, 'retries': 0, 'success': False,
                                           'timeout': step.timeout, 'executed': False})
                context.rollback()
                return WorkflowResult(False, "已取消", "cancelled", step.id,
                                      time.monotonic() - started, tuple(completed),
                                      step_stats=stats)
            action = self.actions[step.id]
            last = StepResult.fail("步骤未执行")
            step_elapsed = 0.0
            step_attempts = 0
            for attempt in range(1, step.retries + 2):
                context.current_step, context.current_attempt = step, attempt
                context.deadline = time.monotonic() + step.timeout
                context.publish({"type": "step", "status": "running", "step": index,
                                 "total": total, "step_id": step.id, "attempt": attempt,
                                 "message": self.catalog[step.id]["name"]})
                attempt_started = time.monotonic()
                try:
                    last = action(context, step)
                    if not isinstance(last, StepResult):
                        raise TypeError(f"step {step.id} returned an invalid result")
                except Exception as exc:
                    logger.exception("Workflow step crashed: %s", step.id)
                    last = StepResult.fail(f"{self.catalog[step.id]['name']}异常: {exc}",
                                           code="exception", retryable=True)
                # 只累计 action 实际执行时间，重试等待不计入节点耗时
                step_elapsed += time.monotonic() - attempt_started
                step_attempts = attempt
                if last.success:
                    completed.append(step.id)
                    context.publish({"type": "step", "status": "success", "step": index,
                                     "total": total, "step_id": step.id, "attempt": attempt,
                                     "message": last.message})
                    break
                if not (last.retryable and attempt <= step.retries and not context.cancelled()):
                    break
                delay = min(step.retry_delay * (2 ** (attempt - 1)), 10.0, context.remaining(10.0))
                context.publish({"type": "step", "status": "retrying", "step": index,
                                 "total": total, "step_id": step.id, "attempt": attempt,
                                 "message": f"{last.message}，{delay:g} 秒后重试"})
                if not _wait(delay, context.cancelled):
                    last = StepResult.fail("已取消", code="cancelled")
                    break
            stats[step.id] = {'elapsed': step_elapsed,
                              'retries': step_attempts - 1,
                              'success': last.success,
                              'timeout': step.timeout,
                              'executed': True}
            if not last.success:
                if step.continue_on_error:
                    completed.append(step.id)
                    continue
                context.rollback()
                context.publish({"type": "step", "status": "error", "step": index,
                                 "total": total, "step_id": step.id, "message": last.message,
                                 "code": last.code, "details": last.details})
                return WorkflowResult(False, last.message, last.code, step.id,
                                      time.monotonic() - started, tuple(completed),
                                      step_stats=stats)
        context.clear_rollbacks()
        return WorkflowResult(True, success_message, "ok", None,
                              time.monotonic() - started, tuple(completed),
                              step_stats=stats)


def _wait(seconds: float, cancelled: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancelled():
            return False
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return not cancelled()
