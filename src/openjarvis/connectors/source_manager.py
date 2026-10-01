"""Instance lifecycle and adapter execution using existing ingestion machinery."""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from typing import Iterator

from openjarvis.connectors._stubs import BaseConnector, Document
from openjarvis.connectors.pipeline import IngestionPipeline
from openjarvis.connectors.source_adapters import get_adapter
from openjarvis.connectors.source_jobs import SourceJobs
from openjarvis.connectors.source_store import SourceConflict, SourceStore
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.connectors.sync_control import JobControl, SyncCancelled
from openjarvis.connectors.sync_engine import SyncEngine

_SYNC_SLOTS = threading.BoundedSemaphore(2)


class _InstanceConnector(BaseConnector):
    def __init__(self, record: dict) -> None:
        self.record = record
        self.connector_id = record["id"]  # independent checkpoint identity
        adapter = get_adapter(record["adapter_id"])
        if record["config_version"] != adapter.config_version:
            raise ValueError("Source configuration needs an adapter version migration")
        self.reader = adapter.factory(record["config"])
        self.full_snapshot = adapter.full_snapshot
        if adapter.snapshot_config_field:
            self.full_snapshot = self.full_snapshot and (
                record["config"].get(adapter.snapshot_config_field) is True
            )
        self.seen: set[str] = set()
        self.control = None

    def is_connected(self) -> bool:
        return self.reader.is_connected()

    def sync_status(self):
        return self.reader.sync_status()

    def disconnect(self) -> None:
        raise SourceConflict("Remove configured instances through SourceManager")

    def sync(self, *, since=None, cursor=None) -> Iterator[Document]:
        # Local snapshots must include unchanged files so deletion reconciliation
        # only follows a complete, successful traversal. Other adapters can use
        # incremental fetching once they declare their deletion semantics.
        kwargs = {} if self.full_snapshot else {"since": since, "cursor": cursor}
        if self.control:
            self.control.check()
        for doc in self.reader.sync(**kwargs):
            if self.control:
                self.control.check()
                self.control.report(phase="indexing", documents_seen=len(self.seen) + 1)
            prefix = f"source:{self.record['id']}:"
            doc_id = (
                doc.doc_id
                if self.record["legacy_document_ids"]
                else prefix + doc.doc_id
            )
            source_id = (
                doc.source_id
                if self.record["legacy_document_ids"]
                else prefix + doc.source_id
            )
            self.seen.add(doc_id)
            yield replace(
                doc,
                doc_id=doc_id,
                source_id=source_id,
                metadata={
                    **doc.metadata,
                    "source_instance_id": self.record["id"],
                    "source_instance_name": self.record["name"],
                    "adapter_id": self.record["adapter_id"],
                    "config_version": self.record["config_version"],
                },
            )


class SourceManager:
    def __init__(self, store: SourceStore | None = None, *, knowledge_path: str = ""):
        self.store = store or SourceStore()
        self.knowledge_path = knowledge_path
        self._credentials = None
        self.jobs = SourceJobs(self.store)
        self._runner = None
        self._workers = {}
        self._worker_guard = threading.RLock()
        self.job_lock_dir = self.store.path.parent / "source-job-locks"
        self.job_lock_dir.mkdir(mode=0o700, exist_ok=True)
        self.lock_dir = self.store.path.parent / "source-locks"
        self.lock_dir.mkdir(mode=0o700, exist_ok=True)

    @contextmanager
    def _locked(self, source_id: str):
        # UUID validation prevents lock-path traversal. Advisory locks work across
        # server processes and are released by the OS after a crash/restart.
        source_id = str(uuid.UUID(source_id))
        fd = os.open(
            self.lock_dir / source_id, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SourceConflict("Source is busy; wait for sync to finish") from exc
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _job_locked(self, identity):
        identity = str(uuid.UUID(identity))
        fd = os.open(
            self.job_lock_dir / identity, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SourceConflict("Job is busy") from None
            yield
        finally:
            os.close(fd)

    def start_jobs(self):
        from openjarvis.connectors.source_job_runner import SourceJobRunner

        with self._worker_guard:
            if self._runner is None:
                self._runner = SourceJobRunner(self)
            self._runner.start()

    def stop_jobs(self):
        if self._runner:
            self._runner.stop()
        with self._worker_guard:
            workers = list(self._workers.items())
        for identity, _ in workers:
            try:
                self.jobs.cancel(self.jobs.get(identity)["source_id"])
            except (KeyError, SourceConflict):
                pass  # Committing work must finish; never report it cancelled.
        deadline = time.monotonic() + 5
        for _, worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))

    def cancel_sync(self, source_id):
        return self.jobs.cancel(source_id)

    @staticmethod
    def _name(name: str) -> str:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("Source name must contain 1–120 characters")
        return name.strip()

    @property
    def credentials(self):
        if self._credentials is None:
            from openjarvis.connectors.source_credentials import CredentialStore

            self._credentials = CredentialStore(
                self.store.path.with_name("source_credentials.db")
            )
        return self._credentials

    def delete_credential(self, identity: str, revision: int):
        return self.credentials.delete(identity, revision, self.store)

    @contextmanager
    def _credential(self, adapter, config, *, unlock=False):
        identity = config.get("credential_id")
        if not identity:
            yield None
            return
        if not adapter.credential_kinds or adapter.bind_credential is None:
            raise ValueError("Adapter does not support protected credentials")
        with self.credentials.bound(
            identity, config["url"], adapter.credential_kinds
        ) as row:
            yield self.credentials.material(row) if unlock else None

    def create(self, adapter_id: str, name: str, config: dict) -> dict:
        adapter = get_adapter(adapter_id)
        config = adapter.validate_config(config)
        with self._credential(adapter, config):
            return self.store.create(
                adapter_id, self._name(name), config, adapter.config_version
            )

    def _connector(self, record: dict) -> _InstanceConnector:
        if not record["enabled"]:
            raise SourceConflict("Source is disabled; enable it before syncing")
        return _InstanceConnector(record)

    @contextmanager
    def _reading(self, record, control=None):
        adapter = get_adapter(record["adapter_id"])
        with self._credential(adapter, record["config"], unlock=True) as material:
            connector = self._connector(record)
            connector.control = control
            connector.reader.bind_sync_control(control)
            if material:
                adapter.bind_credential(connector.reader, material)
            try:
                yield connector
            finally:
                connector.reader.bind_sync_control(None)
                if material:
                    adapter.bind_credential(connector.reader, None)
                    material.clear()

    def test(self, adapter_id: str, config: dict) -> dict:
        adapter = get_adapter(adapter_id)
        validated = adapter.validate_config(config)
        with self._credential(adapter, validated, unlock=True) as material:
            reader = adapter.factory(validated)
            if material:
                adapter.bind_credential(reader, material)
            try:
                if adapter.probe is not None:
                    return {"ok": True, "config": validated, **adapter.probe(reader)}
                if not reader.is_connected():
                    raise ValueError("Source is unavailable")
                return {"ok": True, "config": validated}
            finally:
                if material:
                    adapter.bind_credential(reader, None)
                    material.clear()

    def update(
        self, source_id: str, revision: int, *, name: str, config: dict, enabled: bool
    ) -> dict:
        name = self._name(name)
        with self._locked(source_id):
            old = self.store.get(source_id)
            if old["revision"] != revision:
                raise SourceConflict("Source changed; refresh before saving")
            adapter = get_adapter(old["adapter_id"])
            if old["config_version"] != adapter.config_version:
                raise ValueError(
                    "Source configuration needs an adapter version migration"
                )
            # Naming/disabling an unavailable source should still work. Validate
            # the directory only when its configuration changes or it is enabled.
            if config != old["config"] or (enabled and not old["enabled"]):
                config = adapter.validate_config(config)
            with self._credential(adapter, config):
                if config != old["config"]:
                    self._reset_and_purge(old)
                result = self.store.update(
                    source_id, revision, name=name, config=config, enabled=enabled
                )
                self.store.set_status(source_id, "idle")
                return {**result, "state": "idle", "error": None}

    @staticmethod
    def _prefix(record: dict) -> str:
        if record["legacy_document_ids"]:
            root_id = hashlib.sha256(record["config"]["path"].encode()).hexdigest()[:16]
            return f"local_files:{root_id}:"
        return f"source:{record['id']}:"

    def _reset_and_purge(self, record: dict) -> None:
        with KnowledgeStore(self.knowledge_path) as knowledge:
            with SyncEngine(
                IngestionPipeline(knowledge), state_db=str(self.store.path)
            ) as engine:
                # Reset first: if cleanup fails, a retry can safely re-read the
                # existing source instead of skipping already-purged documents.
                engine.reset_checkpoint(record["id"])
            knowledge.reconcile_document_prefix(self._prefix(record), set())

    def delete(self, source_id: str, revision: int) -> None:
        with self._locked(source_id):
            record = self.store.get(source_id)
            if record["revision"] != revision:
                raise SourceConflict("Source changed; refresh before removing")
            self._reset_and_purge(record)
            self.store.delete(source_id)

    def sync(self, source_id: str) -> int:
        with self._locked(source_id):
            return self._sync_locked(source_id)

    def _sync_locked(self, source_id: str, control=None) -> int:
        self.store.set_status(source_id, "syncing")
        try:
            with self._reading(self.store.get(source_id), control) as connector:
                return self._ingest(connector)
        except SyncCancelled:
            self.store.set_status(source_id, "cancelled")
            raise
        except Exception as exc:
            self.store.set_status(source_id, "error", str(exc)[:1000])
            raise

    def _ingest(self, connector):
        source_id = connector.record["id"]
        with KnowledgeStore(self.knowledge_path) as knowledge:
            with SyncEngine(
                IngestionPipeline(knowledge), state_db=str(self.store.path)
            ) as engine:

                def complete():
                    if connector.control:
                        connector.control.report(
                            force=True,
                            documents_seen=len(connector.seen),
                            documents_total=len(connector.seen),
                        )
                        self.jobs.begin_commit(connector.control.identity)
                    # Readers stage/validate every page before yielding. Ingest
                    # first, then apply explicit deletions or snapshot cleanup.
                    if connector.full_snapshot:
                        knowledge.reconcile_document_prefix(
                            self._prefix(connector.record), connector.seen
                        )
                    deleted = getattr(connector.reader, "deleted_document_ids", set())
                    knowledge.delete_documents(
                        {f"source:{source_id}:{identity}" for identity in deleted}
                    )
                    return connector.reader.sync_status().cursor

                def progress(chunks):
                    if connector.control:
                        connector.control.report(force=True, chunks_written=chunks)

                chunks = engine.sync(
                    connector,
                    on_complete=complete,
                    cancel_event=connector.control,
                    on_progress=progress,
                )
                if connector.control and connector.control.is_set():
                    raise SyncCancelled()
        self.store.set_status(source_id, "idle")
        return chunks

    def start_sync(self, source_id: str, *, trigger="manual", now=None, job_id=None):
        lock = self._locked(source_id)
        lock.__enter__()
        slot_acquired, job_lock, identity = False, None, job_id
        claimed = False
        slots = _SYNC_SLOTS
        try:
            record = self.store.get(source_id)
            if job_id:
                queued = self.jobs.get(job_id)
                if (
                    queued["source_revision"] != record["revision"]
                    or not record["enabled"]
                ):
                    self.jobs.finish(
                        job_id,
                        "cancelled",
                        "Source changed or was disabled before the queued sync started",
                    )
                    lock.__exit__(None, None, None)
                    return self.jobs.get(job_id)
            if not record["enabled"]:
                raise SourceConflict("Source is disabled; enable it before syncing")
            slot_acquired = slots.acquire(blocking=False)
            if not slot_acquired:
                raise SourceConflict("Two sources are syncing; retry when one finishes")
            if identity is None:
                identity = self.jobs.create(record, trigger=trigger, now=now)["id"]
            job_lock = self._job_locked(identity)
            job_lock.__enter__()
            try:
                self.jobs.claim(identity)
            except SourceConflict:
                queued = self.jobs.get(identity)
                if queued["state"] == "queued":
                    job_lock.__exit__(None, None, None)
                    lock.__exit__(None, None, None)
                    slots.release()
                    return queued
                raise
            claimed = True
            self.store.set_status(source_id, "syncing")

            def run():
                control = JobControl(self.jobs, identity)
                try:
                    self._sync_locked(source_id, control)
                    self.jobs.finish(identity, "succeeded")
                except SyncCancelled:
                    self.jobs.finish(identity, "cancelled")
                except Exception as exc:
                    self.jobs.finish(identity, "failed", str(exc))
                finally:
                    job_lock.__exit__(None, None, None)
                    lock.__exit__(None, None, None)
                    slots.release()
                    with self._worker_guard:
                        self._workers.pop(identity, None)

            worker = threading.Thread(
                target=run, name=f"source-sync-{identity}", daemon=True
            )
            with self._worker_guard:
                self._workers[identity] = worker
                worker.start()
            return self.jobs.get(identity)
        except Exception:
            with self._worker_guard:
                self._workers.pop(identity, None)
            if claimed:
                self.jobs.finish(identity, "failed", "Sync worker could not start")
            if job_lock is not None:
                job_lock.__exit__(None, None, None)
            lock.__exit__(None, None, None)
            if slot_acquired:
                slots.release()
            raise

    def list(self) -> list[dict]:
        results = []
        for record in self.store.list():
            # Persisted 'syncing' without an OS lock means the old worker died.
            if record["state"] == "syncing":
                try:
                    with self._locked(record["id"]):
                        self.store.set_status(
                            record["id"], "error", "Sync interrupted; retry"
                        )
                        record = self.store.get(record["id"])
                except SourceConflict:
                    pass
                except KeyError:
                    continue  # removed after the initial list snapshot
            with KnowledgeStore(self.knowledge_path) as knowledge:
                record["chunks"] = knowledge.count_document_prefix(self._prefix(record))
                with SyncEngine(
                    IngestionPipeline(knowledge), state_db=str(self.store.path)
                ) as engine:
                    record["checkpoint"] = engine.get_checkpoint(record["id"])
            try:
                record["schedule"] = self.jobs.schedule(record["id"])
                history = self.jobs.history(record["id"], limit=1)
            except KeyError:
                continue
            record["latest_job"] = history[0] if history else None
            record.pop("legacy_document_ids")
            results.append(record)
        return results

    def collect(self, adapter_id: str, *, since=None) -> Iterator[Document]:
        """Tool-side reads preserve adapter capability checks in the caller."""
        for record in self.store.list():
            if record["adapter_id"] != adapter_id or not record["enabled"]:
                continue
            with self._locked(record["id"]):
                current = self.store.get(record["id"])
                with self._reading(current) as connector:
                    for doc in connector.sync():
                        if since is None or doc.timestamp.replace(
                            tzinfo=None
                        ) >= since.replace(tzinfo=None):
                            yield doc


class ManagedSourcesReader:
    """Read enabled instances through the existing capability-governed digest tool."""

    def __init__(self, adapter_id: str) -> None:
        self.manager = SourceManager()
        self.adapter_id = adapter_id

    def is_connected(self) -> bool:
        return any(
            record["adapter_id"] == self.adapter_id and record["enabled"]
            for record in self.manager.store.list()
        )

    def sync(self, *, since=None, **kwargs) -> Iterator[Document]:
        yield from self.manager.collect(self.adapter_id, since=since)
