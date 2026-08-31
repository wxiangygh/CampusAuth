"""工作流节点计时统计与超时/重试自动调优。

算法说明
--------
**超时估计采用 RFC 6298（Jacobson/Karels）**，即 TCP RTO 的经典算法：

    SRTT'  = (7/8)·SRTT  + (1/8)·R          平滑平均耗时
    RTTVAR = (3/4)·RTTVAR + (1/4)·|SRTT - R| 平滑平均偏差
    RTO    = SRTT + max(G, 4·RTTVAR)

该算法在"快速"（贴近期望值）与"安全冗余"（4 倍偏差覆盖正态假设下 99.9% 的
波动）之间取得平衡，是网络领域最成熟的超时自学习方案。我们在 RTO 之上再叠加
25% 的相对余量，应对校园网环境的突发抖动。

**重试数建议采用几何分布模型**：若单节点失败率为 p，重试 k 次后的累计成功率
为 1-(1-p)^(k+1)。给定目标成功率 95%：

    k = ceil( ln(0.05) / ln(1-p) ) - 1

失败率越高需要的重试越多，但受 StepSpec 上限 5 约束——失败率过高时重试本身
已不经济，应留给人工介入。

**稳定度评分**（供前端着色）：重试的影响因子高于耗时（0.7 : 0.3），平均重试
2 次即扣满重试分；耗时达到该节点配置超时的 80% 扣满耗时分。

持久化
------
统计保存在 exe 同目录的 ``workflow_tuning.json``，按 workflow_id → step_id 两级
聚合。所有写入原子化，读失败时静默回退到空统计（不影响主流程）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger('wifi_tray')

# ---- 算法参数 ----
ALPHA_SRTT = 0.125          # RFC 6298: SRTT 平滑系数
BETA_RTTVAR = 0.25         # RFC 6298: RTTVAR 平滑系数
RTTVAR_GAIN = 4.0           # RTO = SRTT + max(G, 4·RTTVAR)
CLOCK_GRANULARITY = 0.5     # G：算法时钟粒度（秒）
TIMEOUT_MARGIN_RATIO = 0.25  # RTO 之上再叠加 25% 相对余量
MIN_TIMEOUT, MAX_TIMEOUT = 2.0, 180.0   # 与 StepSpec 夹逼范围一致
MAX_RETRIES = 5             # 与 StepSpec.retries 上限一致
TARGET_SUCCESS_RATE = 0.95  # 重试数建议的目标累计成功率
RECENT_WINDOW = 20           # 成败环形窗口长度
MIN_SAMPLES_TIMEOUT = 3     # 样本不足时不给超时建议
MIN_SAMPLES_RETRIES = 5     # 样本不足时不给重试建议
CHANGE_RATIO = 0.15         # 建议值与当前值差异 < 15% 视为未变（防抖）

# ---- 稳定度评分权重（重试影响 > 耗时）----
SCORE_RETRY_WEIGHT = 0.7
SCORE_TIME_WEIGHT = 0.3
SCORE_RETRY_SATURATION = 2.0    # 平均重试达到 2 次即扣满重试分
SCORE_TIME_SATURATION = 0.8     # 耗时达到配置超时的 80% 即扣满耗时分


def _empty_step_state() -> dict[str, Any]:
    return {
        'srtt': 0.0, 'rttvar': 0.0, 'samples': 0,
        'runs': 0, 'failures': 0, 'retries_total': 0,
        'recent': [],           # 最近 RECENT_WINDOW 次运行，1=失败 0=成功
        'last_timeout': 0.0,   # 最近一次观察到的该节点超时配置
    }


def stability_score(avg_retries: float, avg_elapsed: float, timeout: float) -> int:
    """节点稳定度评分（0-100）。

    重试次数的影响权重 0.7、耗时 0.3——重试意味着该节点本身不稳定，
    而耗时可能只是特性（比如等待 IPv6 本来就慢）。
    """
    retry_factor = min(1.0, max(0.0, avg_retries) / SCORE_RETRY_SATURATION)
    time_base = max(1.0, float(timeout or 0.0)) * SCORE_TIME_SATURATION
    time_factor = min(1.0, max(0.0, avg_elapsed) / time_base)
    return round(100 * (1 - (SCORE_RETRY_WEIGHT * retry_factor
                             + SCORE_TIME_WEIGHT * time_factor)))


def suggest_retries_from_stats(failure_rate: float, avg_retries: float) -> int:
    """几何分布模型：达到目标成功率所需的重试次数。

    成功率 p 的节点重试 k 次的累计成功率为 1-(1-p)^(k+1)。失败率为 0 时
    无需重试；失败率过高时结果会被上限夹住，交给人工处理。
    """
    if failure_rate <= 0:
        base = 0
    else:
        # ln(1-0.95) = ln(0.05)，除以 ln(1-p) 再减去首次尝试
        try:
            base = math.ceil(math.log(1 - TARGET_SUCCESS_RATE) / math.log(1 - failure_rate)) - 1
        except (ValueError, ZeroDivisionError):
            base = MAX_RETRIES
    # 安全冗余：不低于观测到的平均重试（向上取整），保证经历过重试的节点有冗余
    floor = math.ceil(avg_retries) if avg_retries > 0 else 0
    return max(0, min(MAX_RETRIES, max(base, floor)))


class WorkflowTuningStore:
    """按 (workflow_id, step_id) 聚合的运行统计与调优建议。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    # ===== 持久化 =====
    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning('[tuning] 统计文件读取失败，从空状态开始: %s', exc)
                return
            if isinstance(raw, dict):
                workflows = raw.get('workflows')
                if isinstance(workflows, dict):
                    self._data = workflows

    def _save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps({'version': 1, 'workflows': self._data},
                                     ensure_ascii=False, indent=2)
                temp = self.path.with_name(
                    f'.{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
                with temp.open('w', encoding='utf-8', newline='\n') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self.path)
            except OSError as exc:
                logger.warning('[tuning] 统计文件写入失败: %s', exc)

    # ===== 记录 =====
    def record(self, workflow_id: str, step_id: str, elapsed: float,
               retries: int, success: bool, timeout: float = 0.0) -> None:
        """记录一次节点运行：elapsed 为各次尝试执行时间之和（不含重试间隔）。"""
        if not workflow_id or not step_id or elapsed < 0:
            return
        workflow_id, step_id = str(workflow_id), str(step_id)
        with self._lock:
            workflow = self._data.setdefault(workflow_id, {})
            state = workflow.setdefault(step_id, _empty_step_state())
            if state['samples'] == 0:
                state['srtt'] = elapsed
                state['rttvar'] = elapsed / 2
            else:
                srtt = state['srtt']
                state['rttvar'] = ((1 - BETA_RTTVAR) * state['rttvar']
                                   + BETA_RTTVAR * abs(srtt - elapsed))
                state['srtt'] = (1 - ALPHA_SRTT) * srtt + ALPHA_SRTT * elapsed
            state['samples'] += 1
            state['runs'] += 1
            if not success:
                state['failures'] += 1
            state['retries_total'] += max(0, int(retries))
            if timeout:
                state['last_timeout'] = float(timeout)
            recent = deque(state['recent'], maxlen=RECENT_WINDOW)
            recent.append(0 if success else 1)
            state['recent'] = list(recent)
            self._save()

    # ===== 查询 =====
    def _state(self, workflow_id: str, step_id: str) -> dict[str, Any] | None:
        return self._data.get(str(workflow_id), {}).get(str(step_id))

    def suggest_timeout(self, workflow_id: str, step_id: str,
                        current: float | None = None) -> float | None:
        """RFC 6298 RTO + 25% 余量；样本不足或与当前值差异太小则返回 None。"""
        state = self._state(workflow_id, step_id)
        if not state or state['samples'] < MIN_SAMPLES_TIMEOUT:
            return None
        rto = state['srtt'] + max(CLOCK_GRANULARITY, RTTVAR_GAIN * state['rttvar'])
        suggestion = rto * (1 + TIMEOUT_MARGIN_RATIO)
        suggestion = max(MIN_TIMEOUT, min(MAX_TIMEOUT, suggestion))
        if current and abs(suggestion - current) / max(current, 1.0) < CHANGE_RATIO:
            return None
        return round(suggestion, 1)

    def suggest_retries(self, workflow_id: str, step_id: str,
                        current: int | None = None) -> int | None:
        """几何分布模型；样本不足或与当前值相同则返回 None。"""
        state = self._state(workflow_id, step_id)
        if not state or state['runs'] < MIN_SAMPLES_RETRIES:
            return None
        recent = state['recent'] or [0]
        failure_rate = sum(recent) / len(recent)
        avg_retries = state['retries_total'] / max(1, state['runs'])
        suggestion = suggest_retries_from_stats(failure_rate, avg_retries)
        if current is not None and suggestion == int(current):
            return None
        return suggestion

    def stats(self, workflow_id: str, step_id: str) -> dict[str, Any]:
        """前端展示用的统计快照。"""
        state = self._state(workflow_id, step_id)
        if not state or state['runs'] == 0:
            return {'runs': 0}
        runs = state['runs']
        avg_retries = state['retries_total'] / runs
        timeout = state['last_timeout'] or 15.0
        return {
            'runs': runs,
            'avg_elapsed': round(state['srtt'], 2),
            'avg_retries': round(avg_retries, 2),
            'score': stability_score(avg_retries, state['srtt'], timeout),
            'suggested_timeout': self.suggest_timeout(workflow_id, step_id, timeout),
            'suggested_retries': self.suggest_retries(
                workflow_id, step_id, math.ceil(avg_retries)),
        }

    def workflow_stats(self, workflow_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            steps = self._data.get(str(workflow_id), {})
            return {step_id: self.stats(workflow_id, step_id)
                    for step_id in steps}

    # ===== 应用调优 =====
    def apply_to_workflow(self, workflow_id: str,
                          steps: list[dict[str, Any]]) -> bool:
        """把建议的 timeout/retries 写入步骤列表，返回是否有实质变化。"""
        changed = False
        for step in steps:
            step_id = str(step.get('id', ''))
            timeout = float(step.get('timeout', 15) or 15)
            new_timeout = self.suggest_timeout(workflow_id, step_id, timeout)
            if new_timeout is not None:
                step['timeout'] = new_timeout
                changed = True
            retries = int(step.get('retries', 0) or 0)
            new_retries = self.suggest_retries(workflow_id, step_id, retries)
            if new_retries is not None and new_retries != retries:
                step['retries'] = new_retries
                changed = True
        return changed


# ---------------------------------------------------------------------------
# 全局单例（与 core.config 同模式：由 tray_app 在启动时配置路径）
# ---------------------------------------------------------------------------

_store: WorkflowTuningStore | None = None
_store_lock = threading.Lock()


def configure_tuning(path: str | Path) -> WorkflowTuningStore:
    global _store
    with _store_lock:
        if _store is None or _store.path != Path(path):
            _store = WorkflowTuningStore(path)
        return _store


def get_tuning_store() -> WorkflowTuningStore:
    if _store is None:
        raise RuntimeError('workflow tuning store has not been initialized')
    return _store
