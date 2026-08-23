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
import threading
from pathlib import Path
from typing import Any, Callable

from core.secrets import PREFIX as SECRET_PREFIX, protect_text, unprotect_text

logger = logging.getLogger("wifi_tray")

DEFAULT_AUTH_WORKFLOW = [
    {"id": "ensure_wifi", "enabled": True, "retries": 0, "timeout": 10, "retry_delay": 1.0},
    {"id": "prepare_network", "enabled": True, "retries": 0, "timeout": 8, "retry_delay": 1.0},
    {"id": "portal_login", "enabled": True, "retries": 1, "timeout": 8, "retry_delay": 1.5},
    {"id": "configure_ipv6", "enabled": True, "retries": 1, "timeout": 15, "retry_delay": 2.0},
    {"id": "configure_warp", "enabled": True, "retries": 0, "timeout": 8, "retry_delay": 1.0},
    {"id": "connect_warp", "enabled": True, "retries": 1, "timeout": 15, "retry_delay": 2.0},
    {"id": "finalize", "enabled": True, "retries": 0, "timeout": 5, "retry_delay": 1.0},
]

DEFAULT_CONFIG = {
    "username": "", "password": "", "wifi_name": "",
    "auto_auth": False, "auto_startup": False, "auto_restore": False,
    "auto_enable_ipv4": True, "auth_total_timeout": 90,
    "portal_ip": "10.21.221.98", "portal_port": "801",
    "warp_cli_path": "", "silent_startup": False,
    "window_x": None, "window_y": None, "window": None, "ui_prefs": None,
    "auth_workflow": DEFAULT_AUTH_WORKFLOW,
}


def _merge_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(DEFAULT_CONFIG)
    data.update(raw)
    data["auth_workflow"] = copy.deepcopy(data.get("auth_workflow") or DEFAULT_AUTH_WORKFLOW)
    try:
        data["auth_total_timeout"] = max(30, min(int(data.get("auth_total_timeout", 90)), 300))
    except (TypeError, ValueError):
        data["auth_total_timeout"] = 90
    return data


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
            next_data = _merge_defaults(next_data)
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
