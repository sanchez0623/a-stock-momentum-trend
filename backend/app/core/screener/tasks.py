"""异步扫描任务管理(内存态, 重启即清)."""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from typing import Any


class ScanTaskManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}

    def create(self, market: str, top_n: int) -> str:
        task_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "status": "pending",  # pending/running/done/failed
                "market": market,
                "top_n": top_n,
                "total": 0,
                "done": 0,
                "progress": 0,
                "result": [],
                "error": "",
                "created_at": dt.datetime.now().strftime("%H:%M:%S"),
            }
        return task_id

    def update(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(fields)

    def progress(self, task_id: str, done: int, total: int) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["done"] = done
                self._tasks[task_id]["total"] = total
                self._tasks[task_id]["progress"] = round(done / total * 100, 1) if total else 0

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return dict(t) if t else None

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._tasks:
                return None
            item = max(self._tasks.values(), key=lambda t: t["created_at"])
            return dict(item)
