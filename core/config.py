"""Thread-safe, atomic application configuration.

Core modules read configuration here instead of importing ``tray_app``. This
keeps dependencies one-way and prevents stale dictionaries from overwriting
newer values written by another callback or worker thread.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from core.secrets import PREFIX as SECRET_PREFIX, protect_text, unprotect_text

logger = logging.getLogger("wifi_tray")


def _step(step_id: str, *, enabled: bool = True, retries: int = 0,
          timeout: float = 15.0, retry_delay: float = 1.0,
          continue_on_error: bool = False) -> dict[str, Any]:
    return {
        "id": step_id,
        "enabled": enabled,
        "retries": retries,
        "timeout": timeout,
        "retry_delay": retry_delay,
        "continue_on_error": continue_on_error,
    }


# The default authentication workflow is intentionally split into small,
# independently composable nodes. Composite legacy nodes remain available for
# configurations saved by older versions.
DEFAULT_AUTH_WORKFLOW = [
    _step("ensure_wifi", timeout=10),
    _step("detect_warp_state", timeout=4),
    _step("disconnect_warp", timeout=8),
    _step("enable_ipv4", timeout=8),
    _step("portal_login", retries=1, timeout=8, retry_delay=1.5),
    _step("configure_ipv6_dns", timeout=5),
    _step("disable_ipv4", timeout=8),
    _step("wait_public_ipv6", retries=1, timeout=15, retry_delay=2.0),
    _step("set_warp_endpoint_ipv6", timeout=5),
    _step("set_warp_masque", timeout=5),
    _step("start_warp_service", timeout=8),
    _step("connect_warp", retries=1, timeout=15, retry_delay=2.0),
    _step("reset_warp_masque", timeout=5),
    _step("reset_warp_endpoint_ipv6", timeout=5),
    _step("enable_ipv4", timeout=8),
    _step("reset_ipv6_dns", timeout=5),
    _step("refresh_status", timeout=4),
]

DEFAULT_PORTAL_LOGOUT_WORKFLOW = [_step("portal_logout", timeout=6)]
DEFAULT_REAUTH_WORKFLOW = [*copy.deepcopy(DEFAULT_PORTAL_LOGOUT_WORKFLOW),
                           *copy.deepcopy(DEFAULT_AUTH_WORKFLOW)]
DEFAULT_RESTART_WARP_WORKFLOW = [
    _step("restart_warp_service", retries=1, timeout=12, retry_delay=1.5),
    _step("connect_warp", retries=1, timeout=15, retry_delay=2.0),
    _step("refresh_status", timeout=4),
]

# 恢复正常网络模式：断开 WARP、停止其服务并解除底层 IPv6 pin，
# 重新启用校园网 IPv4 直连。与主页「恢复网络」按钮绑定的
# restore_button_workflow 配合使用（空串时走内置 run_restore_task）。
DEFAULT_RESTORE_WORKFLOW = [
    _step("disconnect_warp", timeout=8),
    _step("stop_warp_service", timeout=8),
    _step("warp_underlay_unpin", timeout=8),
    _step("enable_ipv4", timeout=8),
    _step("reset_ipv6_dns", timeout=5),
    _step("refresh_status", timeout=4),
]


def _builtin_workflows() -> dict[str, dict[str, Any]]:
    return {
        "default_auth": {
            "id": "default_auth",
            "name": "完整校园网认证",
            "description": "从 WiFi 检查、Portal 认证、IPv6 准备到 WARP 连接的完整流程。",
            "built_in": True,
            "tray_menu": True,
            "steps": copy.deepcopy(DEFAULT_AUTH_WORKFLOW),
        },
        "portal_reauth": {
            "id": "portal_reauth",
            "name": "注销并重新认证",
            "description": "先注销校园网 Portal，再执行完整认证流程。",
            "built_in": True,
            "tray_menu": True,
            "steps": copy.deepcopy(DEFAULT_REAUTH_WORKFLOW),
        },
        "portal_logout": {
            "id": "portal_logout",
            "name": "注销校园网",
            "description": "仅执行 Portal 注销，可与其他节点自由组合。",
            "built_in": True,
            "tray_menu": True,
            "steps": copy.deepcopy(DEFAULT_PORTAL_LOGOUT_WORKFLOW),
        },
        "restart_warp": {
            "id": "restart_warp",
            "name": "重启 Cloudflare WARP",
            "description": "独立重启 CloudflareWARP 服务并重新连接 WARP。",
            "built_in": True,
            "tray_menu": True,
            "steps": copy.deepcopy(DEFAULT_RESTART_WARP_WORKFLOW),
        },
        "restore_network": {
            "id": "restore_network",
            "name": "恢复网络（IPv4 直连）",
            "description": "断开 WARP 并停止其服务、解除底层 IPv6 pin，"
                           "恢复校园网 IPv4 直连。可在设置页绑定到「恢复网络」按钮。",
            "built_in": True,
            "tray_menu": True,
            "steps": copy.deepcopy(DEFAULT_RESTORE_WORKFLOW),
        },
    }


DEFAULT_CONFIG = {
    "username": "", "password": "", "wifi_name": "",
    "auto_auth": False, "auto_startup": False, "auto_restore": False,
    "auto_enable_ipv4": True, "auth_total_timeout": 90,
    # WARP 底层网络锁定在校园网 IPv6 上。
    # WARP 的端点配置里同时有 v4/v6 地址，只要网卡 IPv4 可用它就会优先走 v4 端点，
    # 底层流量因此变成计费的校园网 IPv4（且 Portal 会话过期后会连带断线）。
    # 开启后会清空 Cloudflare conf.json 里的 IPv4 端点，强制底层只走免费 IPv6。
    "warp_underlay_ipv6": True,
    # WARP 意外断开后自动重连（仅对非主动断开生效）
    "warp_auto_reconnect": False,
    # 判定"意外断开"的持续时长（秒），低于该值视为正常抖动/切换
    "warp_reconnect_delay": 20,
    "portal_ip": "10.21.221.98", "portal_port": "801",
    "warp_cli_path": "", "silent_startup": False,
    # 启动时自动检测 GitHub Releases 更新
    "auto_check_update": True,
    # 按运行数据自动调优各工作流节点的超时与重试设置
    "auto_tune_workflow": False,
    # 首次运行记录的安装目录，后续更新始终覆盖安装到同一目录
    "install_dir": None,
    "window_x": None, "window_y": None, "window": None, "ui_prefs": None,
    "active_workflow_id": "default_auth",
    "workflows": _builtin_workflows(),
    "auth_workflow": DEFAULT_AUTH_WORKFLOW,
    # 主页按钮绑定的工作流：开始认证 / 恢复网络
    # restore_button_workflow 为空串时使用内置恢复逻辑（run_restore_task）
    "auth_button_workflow": "default_auth",
    "restore_button_workflow": "",
}


def _normalize_steps(steps: Any, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not steps:
        return copy.deepcopy(fallback)
    normalized: list[dict[str, Any]] = []
    for item in steps:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id", "")).strip()
        if not step_id:
            continue
        normalized.append(copy.deepcopy(item))
    return normalized or copy.deepcopy(fallback)


def _normalize_workflow(value: Any, workflow_id: str,
                        fallback: dict[str, Any]) -> dict[str, Any]:
    base = copy.deepcopy(fallback)
    if not isinstance(value, dict):
        return base
    result = copy.deepcopy(base)
    result["id"] = workflow_id
    name = str(value.get("name") or base.get("name") or workflow_id).strip()
    result["name"] = name[:60]
    result["description"] = str(value.get("description") or base.get("description") or "")[:200]
    result["tray_menu"] = bool(value.get("tray_menu", base.get("tray_menu", True)))
    result["built_in"] = bool(base.get("built_in", value.get("built_in", False)))
    customized = bool(value.get("customized", False))
    result["customized"] = customized
    fallback_steps = base["steps"]
    stored_steps = value.get("steps")
    if result["built_in"] and not customized:
        result["steps"] = copy.deepcopy(fallback_steps)
    else:
        result["steps"] = _normalize_steps(stored_steps, fallback_steps)
    return result


def _normalize_workflows(raw: dict[str, Any], legacy_workflow: Any,
                          raw_workflows: Any = None,
                          active_override: str | None = None) -> tuple[dict[str, Any], str]:
    workflows = _builtin_workflows()
    if isinstance(raw_workflows, dict):
        for workflow_id, stored in raw_workflows.items():
            workflow_id = str(workflow_id).strip()
            if not workflow_id:
                continue
            fallback = workflows.get(workflow_id, {
                "id": workflow_id,
                "name": workflow_id,
                "description": "",
                "built_in": False,
                "tray_menu": True,
                "steps": _normalize_steps(legacy_workflow, DEFAULT_AUTH_WORKFLOW),
            })
            workflows[workflow_id] = _normalize_workflow(stored, workflow_id, fallback)
    elif legacy_workflow and legacy_workflow != DEFAULT_AUTH_WORKFLOW:
        # Upgrade versions that only had a single editable auth_workflow.
        workflows["legacy_auth"] = {
            "id": "legacy_auth",
            "name": "旧版认证工作流",
            "description": "从旧版本自动迁移。",
            "built_in": False,
            "tray_menu": False,
            "steps": _normalize_steps(legacy_workflow, DEFAULT_AUTH_WORKFLOW),
        }
        active_override = "legacy_auth"

    active = str(active_override or raw.get("active_workflow_id") or "default_auth")
    if active not in workflows:
        active = "default_auth"
    return workflows, active


def _sync_active_workflow(data: dict[str, Any], *, auth_workflow_is_source: bool = False) -> dict[str, Any]:
    active_id = data.get("active_workflow_id", "default_auth")
    workflows = data.setdefault("workflows", _builtin_workflows())
    if active_id not in workflows:
        active_id = "default_auth"
        data["active_workflow_id"] = active_id
    active = workflows[active_id]
    if auth_workflow_is_source and isinstance(data.get("auth_workflow"), list) and data["auth_workflow"]:
        active["steps"] = _normalize_steps(data["auth_workflow"], active["steps"])
        if active.get("built_in"):
            active["customized"] = True
    else:
        data["auth_workflow"] = copy.deepcopy(active["steps"])
    return data


def _merge_defaults(raw: dict[str, Any], *, auth_workflow_is_source: bool | None = None) -> dict[str, Any]:
    data = copy.deepcopy(DEFAULT_CONFIG)
    data.update(raw)
    inferred_auth_source = isinstance(raw.get("auth_workflow"), list) and bool(raw.get("auth_workflow"))
    explicit_auth_workflow = (inferred_auth_source if auth_workflow_is_source is None
                              else auth_workflow_is_source)
    legacy_active = "legacy_auth" if (not isinstance(raw.get("workflows"), dict)
                                      and isinstance(raw.get("auth_workflow"), list)
                                      and raw.get("auth_workflow") != DEFAULT_AUTH_WORKFLOW) else None
    workflows, active_id = _normalize_workflows(
        data, data.get("auth_workflow"), raw_workflows=raw.get("workflows"),
        active_override=legacy_active)
    data["workflows"] = workflows
    data["active_workflow_id"] = active_id
    try:
        data["auth_total_timeout"] = max(30, min(int(data.get("auth_total_timeout", 90)), 300))
    except (TypeError, ValueError):
        data["auth_total_timeout"] = 90
    if not isinstance(data.get("ui_prefs"), dict):
        data["ui_prefs"] = {}
    if not explicit_auth_workflow:
        data["auth_workflow"] = copy.deepcopy(workflows[active_id]["steps"])
    return _sync_active_workflow(data, auth_workflow_is_source=explicit_auth_workflow)


def workflow_id_from_name(name: str) -> str:
    """Create a stable, filesystem/tray-safe custom workflow id."""
    value = re.sub(r"[^0-9a-zA-Z_\-]+", "_", str(name).strip()).strip("_").lower()
    return value[:40] or "custom_workflow"


class ConfigStore:
    """Own the live configuration and serialize every atomic update."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any], int, set[str]], None]] = []
        self._revision = 0
        self._data = _merge_defaults({})
        self.reload()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def snapshot(self, include_revision: bool = False) -> dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._data)
            if include_revision:
                result["_revision"] = self._revision
            return result

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def subscribe(self, listener: Callable[[dict[str, Any], int, set[str]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return unsubscribe

    def reload(self) -> dict[str, Any]:
        with self._lock:
            raw: dict[str, Any] = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    logger.error("Failed to load config %s: %s", self.path, exc)
            protect_on_load = False
            stored_password = raw.get("password", "")
            if stored_password:
                try:
                    raw["password"] = unprotect_text(stored_password)
                    protect_on_load = os.name == "nt" and not stored_password.startswith(SECRET_PREFIX)
                except Exception as exc:
                    logger.error("Saved password could not be decrypted: %s", exc)
                    raw["password"] = ""
            if "portal_server" in raw and "portal_ip" not in raw:
                parts = str(raw.pop("portal_server")).rsplit(":", 1)
                raw["portal_ip"] = parts[0]
                raw["portal_port"] = parts[1] if len(parts) > 1 else ""
            self._data = _merge_defaults(raw)
            if protect_on_load:
                self._write_atomic(self._data)
            self._revision += 1
            return copy.deepcopy(self._data)

    def patch(self, changes: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise TypeError("configuration patch must be an object")
        clean = {key: copy.deepcopy(value) for key, value in changes.items()
                 if not str(key).startswith("_")}
        with self._lock:
            if expected_revision is not None and int(expected_revision) != self._revision:
                raise ValueError("配置已被其他操作更新，请刷新后重试")
            changed = {key for key, value in clean.items() if self._data.get(key) != value}
            if not changed:
                return self.snapshot(include_revision=True)
            next_data = copy.deepcopy(self._data)
            next_data.update(clean)
            next_data = _merge_defaults(
                next_data, auth_workflow_is_source="auth_workflow" in clean)
            self._write_atomic(next_data)
            self._data = next_data
            self._revision += 1
            snapshot = copy.deepcopy(self._data)
            revision = self._revision
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(copy.deepcopy(snapshot), revision, changed)
            except Exception:
                logger.exception("Config listener failed")
        snapshot["_revision"] = revision
        return snapshot

    def _write_atomic(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        disk_data = copy.deepcopy(data)
        if disk_data.get("password"):
            disk_data["password"] = protect_text(str(disk_data["password"]))
        temp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(disk_data, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary config file: %s", temp_path)


_store: ConfigStore | None = None
_store_lock = threading.Lock()


def configure_config(path: str | Path) -> ConfigStore:
    global _store
    with _store_lock:
        if _store is None or _store.path != Path(path):
            _store = ConfigStore(path)
        return _store


def get_config_store() -> ConfigStore:
    if _store is None:
        raise RuntimeError("configuration store has not been initialized")
    return _store


def get_config() -> dict[str, Any]:
    """Return the latest isolated configuration snapshot."""
    return get_config_store().snapshot()
