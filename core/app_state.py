"""Observable runtime state shared by UI, tray and background workers."""
from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("wifi_tray")


class AppStateHub:
    def __init__(self):
        self._lock = threading.RLock()
        self._revision = 0
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._state: dict[str, Any] = {
            "revision": 0,
            "network": {"status": "unknown", "message": "正在检测网络",
                        "updated_at": 0.0, "checking": False},
            "operation": {"kind": None, "status": "idle", "step": 0, "total": 0,
                          "step_id": None, "message": "", "started_at": None,
                          "updated_at": time.time()},
            "config_revision": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def subscribe(self, listener: Callable[[dict[str, Any]], None], *, replay: bool = True) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)
            snapshot = copy.deepcopy(self._state)
        if replay:
            listener(snapshot)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return unsubscribe

    def update_network(self, status: str, message: str, **details: Any) -> dict[str, Any]:
        value = {"status": status, "message": message, "updated_at": time.time(),
                 "checking": False}
        value.update(details)
        return self._update("network", value)

    def set_network_checking(self) -> dict[str, Any]:
        current = self.snapshot()["network"]
        current["checking"] = True
        return self._update("network", current)

    def update_operation(self, *, kind: str | None = None, status: str | None = None,
                         step: int | None = None, total: int | None = None,
                         step_id: str | None = None, message: str | None = None,
                         details: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.snapshot()["operation"]
        updates = {"kind": kind, "status": status, "step": step, "total": total,
                   "step_id": step_id, "message": message}
        for key, value in updates.items():
            if value is not None:
                current[key] = value
        if details is not None:
            current["details"] = copy.deepcopy(details)
        if status == "running" and not current.get("started_at"):
            current["started_at"] = time.time()
        if status in {"success", "error", "cancelled", "idle"}:
            current["finished_at"] = time.time()
        current["updated_at"] = time.time()
        return self._update("operation", current)

    def set_config_revision(self, revision: int) -> dict[str, Any]:
        return self._update("config_revision", int(revision))

    def _update(self, key: str, value: Any) -> dict[str, Any]:
        with self._lock:
            self._revision += 1
            self._state[key] = copy.deepcopy(value)
            self._state["revision"] = self._revision
            snapshot = copy.deepcopy(self._state)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(copy.deepcopy(snapshot))
            except Exception:
                logger.exception("App-state listener failed")
        return snapshot


app_state = AppStateHub()
