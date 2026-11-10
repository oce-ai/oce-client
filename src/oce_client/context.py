from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable

from .filesystem import FileAdmissionError, LocalFileSource
from .http import OceApiError
from .identity import Sha256BlobIdentity
from .ignore import LayeredIgnoreMatcher
from .models import (
    BlobDelta,
    BlobUpload,
    FileRecord,
    FileStatus,
    RetrievalResult,
    SyncResult,
    UploadPlan,
    WorkspaceSnapshot,
)
from .ports import BlobApi, BlobIdentity
from .state import SQLiteStateStore
from .watcher import WatchHandle


class CheckpointResetRequired(RuntimeError):
    pass


class BlobCompatibilityError(RuntimeError):
    pass


class WorkspaceContext:
    """Synchronous workspace inventory, blob synchronization, and retrieval facade."""

    def __init__(
        self,
        root: Path,
        api: BlobApi,
        state: SQLiteStateStore,
        *,
        identity: BlobIdentity | None = None,
        file_source: LocalFileSource | None = None,
        runtime_patterns: Iterable[str] = (),
        ready_poll_attempts: int = 20,
        ready_poll_seconds: float = 0.25,
        max_find_missing: int = 1000,
        max_upload_blobs: int = 1000,
        max_upload_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.root = root.resolve()
        self.api = api
        self.state = state
        self.identity = identity or Sha256BlobIdentity()
        self.file_source = file_source or LocalFileSource()
        self.runtime_patterns = tuple(runtime_patterns)
        self._cached_matcher: LayeredIgnoreMatcher | None = None
        self.ready_poll_attempts = ready_poll_attempts
        self.ready_poll_seconds = ready_poll_seconds
        if max_find_missing < 1 or max_upload_blobs < 1 or max_upload_bytes < 1:
            raise ValueError("sync batch limits must be positive")
        self.max_find_missing = max_find_missing
        self.max_upload_blobs = max_upload_blobs
        self.max_upload_bytes = max_upload_bytes
        self._watch: WatchHandle | None = None

    @classmethod
    def open(
        cls,
        root: str | os.PathLike[str],
        api: BlobApi,
        *,
        state_path: str | os.PathLike[str] | None = None,
        identity: BlobIdentity | None = None,
        file_source: LocalFileSource | None = None,
        runtime_patterns: Iterable[str] = (),
        **kwargs: object,
    ) -> "WorkspaceContext":
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            raise NotADirectoryError(root_path)
        db_path = Path(state_path) if state_path else root_path / ".oce-client" / "state.sqlite3"
        return cls(
            root_path,
            api,
            SQLiteStateStore(db_path),
            identity=identity,
            file_source=file_source,
            runtime_patterns=runtime_patterns,
            **kwargs,
        )

    def close(self) -> None:
        if self._watch is not None:
            self._watch.stop()
            self._watch.join(2.0)
            self._watch = None
        self.state.close()
        close = getattr(self.api, "close", None)
        if close is not None:
            close()

    def _matcher(self) -> LayeredIgnoreMatcher:
        # 规则文件（.gitignore/.oceignore）在进程内基本不变，构造要遍历目录树
        # 收集各层 .gitignore（8k 目录约 80ms），因此缓存复用；规则文件本身
        # 被改动时由 refresh_matcher() 显式失效。
        if self._cached_matcher is None:
            self._cached_matcher = LayeredIgnoreMatcher(
                self.root, self.runtime_patterns
            )
        return self._cached_matcher

    def refresh_matcher(self) -> None:
        """丢弃缓存的忽略规则（.gitignore/.oceignore 变更后调用）。"""
        self._cached_matcher = None

    def _next_generation(self) -> int:
        return self.state.load_snapshot().generation + 1

    def _normalize_path(self, path: str | os.PathLike[str]) -> str:
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(self.root)
            except ValueError as exc:
                raise ValueError(f"path is outside workspace: {path}") from exc
        normalized = candidate.as_posix()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized or normalized == "." or normalized.startswith("../"):
            raise ValueError(f"invalid workspace-relative path: {path}")
        return normalized

    def observe_file(self, path: str, content: str) -> None:
        normalized = self._normalize_path(path)
        if self._matcher().ignores(normalized):
            raise ValueError(f"path is ignored: {normalized}")
        if not isinstance(content, str):
            raise TypeError("content must be text")
        if len(content.encode("utf-8")) > self.file_source.max_file_size:
            raise FileAdmissionError(f"file exceeds {self.file_source.max_file_size} bytes")
        if "\x00" in content:
            raise FileAdmissionError("binary content is not supported")
        generation = self._next_generation()
        record = FileRecord(
            normalized,
            self.identity.calculate(normalized, content),
            FileStatus.PRESENT,
            content,
            len(content.encode("utf-8")),
            None,
            "explicit",
            generation,
        )
        self.state.upsert_file(record)
        self.state.set_meta("generation", str(generation))

    def remove_file(self, path: str) -> None:
        normalized = self._normalize_path(path)
        generation = self._next_generation()
        self.state.mark_deleted_paths([normalized], generation)
        self.state.set_meta("generation", str(generation))

    def reconcile(self) -> WorkspaceSnapshot:
        generation = self._next_generation()
        current = self.state.load_snapshot()
        explicit = {
            path: record
            for path, record in current.files.items()
            if record.source == "explicit" and record.status == FileStatus.PRESENT
        }
        records: list[FileRecord] = []
        known_paths: set[str] = set()
        scanned_paths: set[str] = set()
        matcher = self._matcher()

        def flush_records() -> None:
            if records:
                self.state.apply_file_changes(records, [], generation)
                records.clear()

        for path, source_path, stat in self.file_source.iter_files(self.root, matcher):
            scanned_paths.add(path)
            known_paths.add(path)
            existing = current.files.get(path)
            if (
                existing is not None
                and existing.source == "filesystem"
                and existing.status == FileStatus.PRESENT
                and existing.blob_name
                and existing.mtime_ns == stat.st_mtime_ns
                and existing.size == stat.st_size
            ):
                records.append(
                    FileRecord(
                        path,
                        existing.blob_name,
                        FileStatus.PRESENT,
                        None,
                        existing.size,
                        existing.mtime_ns,
                        "filesystem",
                        generation,
                    )
                )
                if len(records) >= 256:
                    flush_records()
                continue
            try:
                content = self.file_source.read(source_path)
                stat = source_path.stat()
            except (FileAdmissionError, OSError):
                continue
            blob_name = self.identity.calculate(path, content)
            overlay = explicit.get(path)
            if overlay is not None and blob_name != overlay.blob_name:
                records.append(overlay)
                if len(records) >= 256:
                    flush_records()
                continue
            records.append(
                FileRecord(
                    path,
                    blob_name,
                    FileStatus.PRESENT,
                    content,
                    stat.st_size,
                    stat.st_mtime_ns,
                    "filesystem",
                    generation,
                )
            )
            if len(records) >= 256:
                flush_records()
        for path, record in explicit.items():
            if path not in scanned_paths:
                records.append(record)
                known_paths.add(path)
                if len(records) >= 256:
                    flush_records()
        flush_records()
        self.state.set_meta("generation", str(generation))
        missing_paths = [path for path in current.files if path not in known_paths and path not in explicit]
        self.state.mark_missing_paths(missing_paths, generation)
        return self.state.load_snapshot()

    def reconcile_paths(self, changed_paths: Iterable[str | os.PathLike[str]]) -> WorkspaceSnapshot:
        resolved_paths: set[Path] = set()
        for path in changed_paths:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            resolved_paths.add(candidate.resolve())
        if not resolved_paths:
            return self.snapshot()
        for path in resolved_paths:
            if path.name in {".gitignore", ".oceignore"} or path.is_dir():
                # 规则文件或目录变更会影响整棵树，走全量 reconcile；
                # 规则文件本身改写时要重建缓存的忽略规则。
                if path.name in {".gitignore", ".oceignore"}:
                    self.refresh_matcher()
                return self.reconcile()

        generation = self._next_generation()
        current = self.state.load_snapshot()
        matcher = self._matcher()
        records: list[FileRecord] = []
        deleted: set[str] = set()
        for source_path in resolved_paths:
            try:
                relative = source_path.relative_to(self.root).as_posix()
            except ValueError:
                continue
            tracked = {
                path
                for path in current.files
                if path == relative or path.startswith(relative.rstrip("/") + "/")
            }
            if not source_path.is_file() or matcher.ignores(relative):
                deleted.update(
                    path
                    for path in tracked
                    if current.files[path].source != "explicit"
                )
                continue
            try:
                stat = source_path.stat()
                if stat.st_size > self.file_source.max_file_size:
                    deleted.update(tracked)
                    continue
                content = self.file_source.read(source_path)
                stat = source_path.stat()
            except (FileAdmissionError, OSError):
                deleted.update(tracked)
                continue
            blob_name = self.identity.calculate(relative, content)
            existing = current.files.get(relative)
            if (
                existing is not None
                and existing.source == "explicit"
                and existing.status == FileStatus.PRESENT
                and existing.blob_name != blob_name
            ):
                records.append(existing)
                continue
            records.append(
                FileRecord(
                    relative,
                    blob_name,
                    FileStatus.PRESENT,
                    content,
                    stat.st_size,
                    stat.st_mtime_ns,
                    "filesystem",
                    generation,
                )
            )
            deleted.discard(relative)
        self.state.apply_file_changes(records, sorted(deleted), generation)
        return self.state.load_snapshot()

    def snapshot(self) -> WorkspaceSnapshot:
        return self.state.load_snapshot()

    def plan_sync(self) -> UploadPlan:
        current_names: set[str] = set()
        committed_names: set[str] = set()
        skipped: list[str] = []
        for row in self.state.load_file_rows():
            if row["status"] == FileStatus.PRESENT.value and row["blob_name"]:
                current_names.add(str(row["blob_name"]))
            elif row["status"] == FileStatus.SKIPPED.value:
                skipped.append(str(row["path"]))
            if row["committed_blob_name"]:
                committed_names.add(str(row["committed_blob_name"]))
        added = tuple(sorted(current_names - committed_names))
        deleted = tuple(sorted(committed_names - current_names))
        return UploadPlan(
            (),
            BlobDelta(self.state.get_meta("checkpoint_id"), added, deleted),
            tuple(sorted(skipped)),
        )

    def _wait_ready(self, names: tuple[str, ...], checkpoint_id: str | None) -> None:
        if not names:
            return
        pending = set(names)
        for attempt in range(self.ready_poll_attempts):
            status = self.api.blob_status(tuple(sorted(pending)), checkpoint_id)
            if status.checkpoint_not_found:
                raise CheckpointResetRequired("server checkpoint no longer exists")
            if status.unknown_blob_names:
                raise OceApiError(404, f"unknown blobs: {status.unknown_blob_names}")
            pending = set(status.nonindexed_blob_names)
            if not pending:
                return
            if attempt + 1 < self.ready_poll_attempts:
                time.sleep(self.ready_poll_seconds)
        raise TimeoutError(f"blobs did not become ready: {sorted(pending)}")

    def sync(self) -> SyncResult:
        self.reconcile()
        return self._sync_reconciled()

    def sync_paths(
        self,
        changed_paths: Iterable[str | os.PathLike[str]],
    ) -> SyncResult:
        self.reconcile_paths(changed_paths)
        return self._sync_reconciled()

    def _sync_reconciled(self) -> SyncResult:
        plan = self.plan_sync()
        payload = {
            "delta": plan.delta.to_api_dict(),
            "uploads": [
                {"path": upload.path, "blob_name": upload.blob_name}
                for upload in plan.uploads
            ],
        }
        # A new sync is a durable retry of any prior failed attempt. The current
        # inventory is authoritative, so retrying from a fresh plan is idempotent.
        self.state.supersede_pending()
        operation_id = self.state.add_outbox("sync", payload)
        try:
            rows = self.state.load_file_rows()
            current_records = {
                str(row["blob_name"]): row
                for row in rows
                if row["status"] == FileStatus.PRESENT.value and row["blob_name"]
            }
            current_names = tuple(sorted(current_records))
            names_to_check = (
                current_names
                if plan.delta.checkpoint_id is None
                else plan.delta.added_blobs
            )
            unknown: set[str] = set()
            nonindexed: set[str] = set()
            for offset in range(0, len(names_to_check), self.max_find_missing):
                missing = self.api.find_missing(
                    names_to_check[offset : offset + self.max_find_missing]
                )
                unknown.update(missing.unknown_blob_names)
                nonindexed.update(missing.nonindexed_blob_names)
            to_upload = unknown | nonindexed
            uploaded_names: set[str] = set()
            batch: list[BlobUpload] = []
            batch_bytes = 0
            for name in sorted(to_upload):
                record = current_records.get(name)
                if record is None:
                    raise BlobCompatibilityError(f"missing local record for blob {name}")
                path = str(record["path"])
                content = self.state.load_file_content(path)
                if content is None:
                    content = self.file_source.read(self.root / Path(path))
                actual_name = self.identity.calculate(path, content)
                if actual_name != name:
                    raise BlobCompatibilityError(f"file changed during sync: {path}")
                upload = BlobUpload(path, content, name)
                upload_bytes = len(upload.path.encode("utf-8")) + len(upload.content.encode("utf-8"))
                if batch and (
                    len(batch) >= self.max_upload_blobs
                    or batch_bytes + upload_bytes > self.max_upload_bytes
                ):
                    result = self.api.batch_upload(batch)
                    expected = {item.blob_name for item in batch}
                    received = set(result.blob_names)
                    if received != expected:
                        raise BlobCompatibilityError(
                            f"batch-upload returned {sorted(received)}; expected {sorted(expected)}"
                        )
                    uploaded_names.update(received)
                    batch = []
                    batch_bytes = 0
                batch.append(upload)
                batch_bytes += upload_bytes
            if batch:
                result = self.api.batch_upload(batch)
                expected = {item.blob_name for item in batch}
                received = set(result.blob_names)
                if received != expected:
                    raise BlobCompatibilityError(
                        f"batch-upload returned {sorted(received)}; expected {sorted(expected)}"
                    )
                uploaded_names.update(received)
            uploaded = tuple(sorted(uploaded_names))
            self._wait_ready(tuple(sorted(to_upload)), None)
            checkpoint_id = plan.delta.checkpoint_id
            try:
                if plan.delta.added_blobs or plan.delta.deleted_blobs or checkpoint_id is None:
                    result = self.api.checkpoint(
                        checkpoint_id,
                        plan.delta.added_blobs,
                        plan.delta.deleted_blobs,
                    )
                    checkpoint_id = result.new_checkpoint_id
            except OceApiError as exc:
                if exc.status_code != 404:
                    raise
                # The server lost the chain. Rebuild the current workspace from scratch.
                self.state.set_meta("checkpoint_id", None)
                result = self.api.checkpoint(None, current_names, ())
                checkpoint_id = result.new_checkpoint_id
            deleted_paths = [
                row["path"]
                for row in self.state.load_file_rows()
                if row["status"] != FileStatus.PRESENT
                and row["committed_blob_name"] in set(plan.delta.deleted_blobs)
            ]
            self.state.commit_sync(checkpoint_id or "", deleted_paths, self.snapshot().generation)
            self.state.update_outbox(operation_id, "complete")
            return SyncResult(uploaded, checkpoint_id, plan.delta.added_blobs, plan.delta.deleted_blobs)
        except Exception as exc:
            self.state.update_outbox(operation_id, "failed", str(exc))
            raise

    def retrieve(self, query: str, *, scope: str = "workspace") -> RetrievalResult:
        if scope not in {"workspace", "working_set"}:
            raise ValueError(f"unknown scope: {scope}")
        if self.state.get_meta("synced_generation") != self.state.get_meta("generation"):
            self.sync()
        snapshot = self.snapshot()
        names = tuple(sorted(record.blob_name for record in snapshot.files.values() if record.status == FileStatus.PRESENT and record.blob_name))
        return self.api.retrieve(query, snapshot.checkpoint_id, names if snapshot.checkpoint_id is None else (), ())

    def start_watching(self, *, debounce_ms: int = 300) -> WatchHandle:
        if self._watch is not None:
            return self._watch
        self._watch = WatchHandle(self.root, self.sync_paths, debounce_ms)
        return self._watch
