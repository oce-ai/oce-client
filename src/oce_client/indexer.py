from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from .ignore import LayeredIgnoreMatcher
from .runtime import ClientRuntime, ClientSettings
from .watcher import WatchHandle


class WorkspaceIndexer:
    """Own one workspace's background synchronization and readiness barrier."""

    def __init__(
        self,
        settings: ClientSettings,
        *,
        runtime_factory: Callable[[ClientSettings], ClientRuntime] = ClientRuntime,
        debounce_ms: int = 500,
    ) -> None:
        self.settings = settings
        self.debounce_ms = debounce_ms
        self._runtime = runtime_factory(settings)
        self._condition = threading.Condition()
        self._context_lock = threading.Lock()
        self._stop = False
        self._started = False
        self._initialized = False
        self._recovery_required = False
        self._full_pending = False
        self._pending_paths: set[Path] = set()
        self._requested_generation = 0
        self._synced_generation = 0
        self._state = "idle"
        self._last_error: str | None = None
        self._watch: WatchHandle | None = None
        self._worker: threading.Thread | None = None
        self._matcher: LayeredIgnoreMatcher | None = None

    @property
    def root(self) -> Path:
        return self.settings.root.resolve()

    def start(self, *, initial_sync: bool = True) -> None:
        with self._condition:
            if self._started:
                if (
                    initial_sync
                    and not self._initialized
                    and self._state in {"idle", "error"}
                    and not self._full_pending
                ):
                    self._request_full_locked()
                return
            self._started = True
            self._watch = WatchHandle(self.root, self.notify_changes, self.debounce_ms)
            self._worker = threading.Thread(
                target=self._run,
                name=f"oce-indexer-{self.root.name}",
                daemon=True,
            )
            self._worker.start()
            if initial_sync:
                self._request_full_locked()

    def stop(self) -> None:
        watch: WatchHandle | None
        worker: threading.Thread | None
        with self._condition:
            if not self._started:
                self._runtime.close()
                return
            self._stop = True
            watch = self._watch
            worker = self._worker
            self._condition.notify_all()
        if watch is not None:
            watch.stop()
            watch.join(2.0)
        if worker is not None:
            worker.join(5.0)
            if worker.is_alive():
                return
        with self._context_lock:
            self._runtime.close()

    def _request_full_locked(self) -> None:
        self._requested_generation += 1
        self._full_pending = True
        self._state = "indexing"
        self._last_error = None
        self._condition.notify_all()

    def request_full_sync(self) -> None:
        with self._condition:
            self._request_full_locked()

    def _ignore_matcher(self) -> LayeredIgnoreMatcher:
        """缓存的忽略规则：构造要遍历目录树收集 .gitignore，watcher 每条事件
        都重建会在十万级目录的仓库上造成明显卡顿。"""
        if self._matcher is None:
            self._matcher = LayeredIgnoreMatcher(
                self.root, self.settings.runtime_patterns
            )
        return self._matcher

    def notify_changes(self, paths: set[Path]) -> None:
        matcher = self._ignore_matcher()
        relevant: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(self.root).as_posix()
            except ValueError:
                continue
            if resolved.name in {".gitignore", ".oceignore"}:
                # 规则文件本身变了：旧规则作废，且必须全量重扫受影响范围
                self._matcher = None
                relevant.add(resolved)
            elif not matcher.ignores(relative, is_dir=resolved.is_dir()):
                relevant.add(resolved)
        if not relevant:
            return
        with self._condition:
            if self._stop:
                return
            self._requested_generation += 1
            if not self._initialized or self._recovery_required or any(
                path.name in {".gitignore", ".oceignore"} for path in relevant
            ):
                self._full_pending = True
            else:
                self._pending_paths.update(relevant)
            self._state = "indexing"
            self._last_error = None
            self._condition.notify_all()

    def _next_batch(self) -> tuple[bool, set[Path], int] | None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._stop or self._full_pending or bool(self._pending_paths)
            )
            if self._stop:
                return None
            full = self._full_pending
            paths = set(self._pending_paths)
            generation = self._requested_generation
            self._full_pending = False
            self._pending_paths.clear()
            self._state = "indexing"
            return full, paths, generation

    def _run(self) -> None:
        while True:
            batch = self._next_batch()
            if batch is None:
                return
            full, paths, generation = batch
            try:
                with self._context_lock:
                    context = self._runtime.context()
                    if full:
                        context.sync()
                    elif paths:
                        context.sync_paths(paths)
            except Exception as exc:
                with self._condition:
                    self._recovery_required = True
                    if self._full_pending or self._pending_paths:
                        self._full_pending = True
                        self._pending_paths.clear()
                    self._state = "error"
                    self._last_error = str(exc)
                    self._condition.notify_all()
                continue
            with self._condition:
                self._initialized = True
                self._recovery_required = False
                self._synced_generation = max(self._synced_generation, generation)
                self._last_error = None
                if self._full_pending or self._pending_paths:
                    self._state = "indexing"
                else:
                    self._state = "ready"
                self._condition.notify_all()

    def _ready_locked(self) -> bool:
        return (
            self._initialized
            and self._state == "ready"
            and not self._full_pending
            and not self._pending_paths
            and self._synced_generation >= self._requested_generation
        )

    def wait_until_ready(self, timeout: float | None) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            if not self._started:
                raise RuntimeError("workspace indexer has not been started")
            if (
                not self._initialized
                and self._state in {"idle", "error"}
                and not self._full_pending
            ):
                self._request_full_locked()
            while not self._ready_locked():
                if (
                    self._state == "error"
                    and not self._full_pending
                    and not self._pending_paths
                ):
                    return "error"
                remaining = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining is not None and remaining <= 0:
                    return "indexing"
                self._condition.wait(remaining)
            return "ready"

    def retrieve(self, query: str, timeout: float) -> dict[str, object]:
        self.start(initial_sync=True)
        with self._condition:
            if (
                self._state == "error"
                and not self._full_pending
                and not self._pending_paths
            ):
                self._request_full_locked()
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            status = self.wait_until_ready(remaining)
            if status != "ready":
                with self._condition:
                    payload: dict[str, object] = {
                        "status": status,
                        "workspace_folder": str(self.root),
                    }
                    if status == "error":
                        payload["error"] = self._last_error or "workspace synchronization failed"
                    else:
                        payload["message"] = "Workspace indexing is still in progress; retry shortly."
                    return payload

            with self._context_lock:
                with self._condition:
                    if not self._ready_locked():
                        continue
                try:
                    result = self._runtime.context().retrieve(query)
                except Exception as exc:
                    return {
                        "status": "error",
                        "workspace_folder": str(self.root),
                        "error": str(exc),
                    }
                return {
                    "status": "ready",
                    "workspace_folder": str(self.root),
                    "formatted_retrieval": result.formatted_retrieval,
                    "elapsed_ms": result.elapsed_ms,
                }

    def status(self) -> dict[str, object]:
        with self._condition:
            return {
                "status": self._state,
                "workspace_folder": str(self.root),
                "requested_generation": self._requested_generation,
                "synced_generation": self._synced_generation,
                "error": self._last_error,
            }
