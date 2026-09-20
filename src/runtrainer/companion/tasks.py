"""长任务存储：AI 聊天/建议在后台线程执行，客户端轮询进度。

幂等根保证：客户端生成 request_id（先落 localStorage 再发）→ 已完成
同 request_id 重试直接返回缓存结果（零重复 AI 计费）；运行中返回同一
task_id；任务 30 分钟过期清理、上限 50 个（超出淘汰最旧）。
"""
from __future__ import annotations

import threading
import time
import uuid

TASK_TTL = 1800.0  # 30 分钟
TASK_MAX = 50


class TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._by_request: dict[str, str] = {}
        self._lock = threading.Lock()

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        for tid in [t for t in self._tasks
                    if now - self._tasks[t]["created"] > TASK_TTL]:
            self._tasks.pop(tid, None)
        self._by_request = {r: t for r, t in self._by_request.items()
                            if t in self._tasks}

    def submit(self, method: str, request_id: str | None,
               fn) -> tuple[str, dict | None, bool]:
        """提交任务。返回 (task_id, 缓存结果|None, 是否新任务)。

        同 request_id 已完成 → 缓存结果；运行中 → 同 task_id 无结果；
        失败过的 request_id 允许重新提交（删旧索引新建任务）。
        """
        with self._lock:
            self._cleanup_locked()
            if request_id:
                tid = self._by_request.get(request_id)
                t = self._tasks.get(tid) if tid else None
                if t:
                    if t["status"] == "done":
                        return tid, t["result"], False
                    if t["status"] == "running":
                        return tid, None, False
                    del self._by_request[request_id]  # error → 允许重试
            if len(self._tasks) >= TASK_MAX:
                oldest = min(self._tasks, key=lambda k: self._tasks[k]["created"])
                self._tasks.pop(oldest, None)
            tid = uuid.uuid4().hex
            self._tasks[tid] = {"method": method, "status": "running",
                                "result": None, "created": time.monotonic()}
            if request_id:
                self._by_request[request_id] = tid
        threading.Thread(target=self._run, args=(tid, fn), daemon=True,
                         name=f"companion-task-{tid[:8]}").start()
        return tid, None, True

    def _run(self, tid: str, fn) -> None:
        try:
            result, status = fn(), "done"
        except Exception as e:  # noqa: BLE001
            result, status = {"ok": False, "error": str(e)}, "error"
        with self._lock:
            t = self._tasks.get(tid)
            if t:
                t["status"], t["result"] = status, result
                t["done_at"] = time.monotonic()

    def get(self, tid: str) -> dict | None:
        with self._lock:
            self._cleanup_locked()
            t = self._tasks.get(tid)
            if t is None:
                return None
            out = {"task_id": tid, "method": t["method"], "status": t["status"]}
            if t["status"] == "done":
                out["result"] = t["result"]
            return out


_store: TaskStore | None = None
_store_lock = threading.Lock()


def get_store() -> TaskStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = TaskStore()
        return _store
