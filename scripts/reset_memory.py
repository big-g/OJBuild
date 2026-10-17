#!/usr/bin/env python3
"""Preview or back up and reset OpenJarvis memory for all accounts.

Run as the normal OpenJarvis service user, using its Python environment.
No changes occur without --apply. Close other Jarvis CLI/SDK processes first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Support a source checkout without installing this maintenance script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SERVICE = "openjarvis-api.service"
SESSION_SQL = {
    "session_messages": 'DELETE FROM "session_messages"',
    "sessions": 'DELETE FROM "sessions"',
    "channel_sessions": 'DELETE FROM "channel_sessions"',
}
AGENT_SQL = {
    name: f'DELETE FROM "{name}"'
    for name in (
        "agent_messages",
        "agent_checkpoints",
        "agent_tasks",
        "agent_learning_log",
    )
}
AGENT_SQL.update(
    {
        "managed_agents": "UPDATE managed_agents SET summary_memory='', "
        "status=CASE WHEN status='running' THEN 'idle' ELSE status END",
        "channel_bindings": "UPDATE channel_bindings SET session_id=NULL",
    }
)
MEMORY_SQL = {
    "documents": "DELETE FROM documents",
    "documents_fts": "DELETE FROM documents_fts",
}


def plan(
    config,
    home,
    *,
    keep_conversations=False,
    include_knowledge=False,
    include_profile=False,
):
    """Resolve effective config paths, including relocated /mnt/ai stores."""
    databases = {}
    texts = set()

    def database(value, statements):
        if value and str(value) != ":memory:":
            path = Path(value).expanduser().resolve()
            databases.setdefault(path, {}).update(statements)

    def text(value):
        if value:
            texts.add(Path(value).expanduser().resolve())

    database(config.memory.db_path, MEMORY_SQL)
    text(config.memory.facts_path)
    text(home / "memory_facts.jsonl")
    text(config.memory_files.memory_path)
    text(home / "MEMORY.md")
    # Named personas can also be selected per request, independent of defaults.
    for path in (home / "personas").glob("*/MEMORY.md"):
        text(path)
    if include_profile:
        text(config.memory_files.user_path)
        text(home / "USER.md")
        for path in (home / "personas").glob("*/USER.md"):
            text(path)
    if not keep_conversations:
        database(config.sessions.db_path, SESSION_SQL)
        database(home / "sessions.db", SESSION_SQL)
        database(config.agent_manager.db_path, AGENT_SQL)
    if include_knowledge:
        # knowledge_chunks DELETE triggers maintain its external-content FTS index.
        database(
            home / "knowledge.db", {"knowledge_chunks": "DELETE FROM knowledge_chunks"}
        )
    protected = {p.resolve() for p in home.glob("*.db")}
    protected.update(
        {
            (home / "config.toml").resolve(),
            Path(config.memory_files.soul_path).expanduser().resolve(),
        }
    )
    if not include_profile:
        protected.add(Path(config.memory_files.user_path).expanduser().resolve())
    if texts & (protected | set(databases)):
        raise ValueError(
            "Memory text path overlaps configuration or a database; reset refused"
        )
    # Catch hard-link aliases too; ordinary symlinks are already resolved above.
    protected_files = [p for p in protected | set(databases) if p.exists()]
    for path in texts:
        if path.exists() and (
            not path.is_file() or any(path.samefile(other) for other in protected_files)
        ):
            raise ValueError(f"Unsafe memory text target: {path}")
    return databases, sorted(texts)


def generated_plan(config, home, databases, *, all_files=False):
    """Delete tracked workflow outputs while retaining uploaded input artifacts."""
    import uuid

    root = Path(config.security.generated_files_dir).expanduser().resolve()
    catalog = root / "catalog.db"
    jobs = root / "workflows" / "jobs.db"
    identities = set()
    if jobs.exists():
        with connect(jobs) as db:
            for (raw,) in db.execute("SELECT result FROM runs"):
                result = json.loads(raw)
                for step in result.get("steps", []):
                    before = step.get("input", {}).get("artifact_id")
                    output = step.get("output", {})
                    for key in ("artifact", "report_artifact"):
                        identity = output.get(key, {}).get("id")
                        if identity and identity != before:
                            identities.add(str(uuid.UUID(identity)))
    paths = []
    if catalog.exists():
        with connect(catalog) as db:
            for identity, owner in db.execute("SELECT id,owner_id FROM artifacts"):
                owner_directory = hashlib.sha256(owner.encode()).hexdigest()
                canonical = str(uuid.UUID(identity))
                if canonical != identity:
                    raise ValueError("Invalid artifact identity")
                if all_files or identity in identities:
                    blob = root / owner_directory / (identity + ".blob")
                    if blob.is_symlink() or blob.parent.is_symlink():
                        raise ValueError("Unsafe artifact path")
                    if blob.exists():
                        paths.append(blob)
                    # UUID parsed above: fixed-format values only, no SQL input.
                    databases.setdefault(catalog, {})[identity] = (
                        f"DELETE FROM artifacts WHERE id='{identity}'"
                    )
    databases.setdefault(jobs, {}).update(
        {"runs": "DELETE FROM runs", "triggers": "DELETE FROM triggers"}
    )
    databases.setdefault(root / "hunyuan" / "jobs.db", {}).update(
        {"terminal_results": "DELETE FROM terminal_results", "jobs": "DELETE FROM jobs"}
    )
    databases.setdefault(home / "auth.db", {}).update(
        {
            "content_reviews": "DELETE FROM content_reviews",
            "reviewed_images": "DELETE FROM reviewed_images",
        }
    )
    return paths


def worker_files(directories):
    files = []
    allowed = {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".glb",
        ".stl",
        ".ply",
        ".obj",
        ".mtl",
        ".json",
        ".txt",
    }
    for directory in directories:
        directory = directory.expanduser().absolute()
        if directory.name not in {"output", "outputs"}:
            raise ValueError("Worker directory must be named output or outputs")
        if directory.resolve() != directory or directory.is_symlink():
            raise ValueError("Worker output symlinks are not supported")
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Symlink in worker outputs: {path}")
            if path.is_dir():
                continue
            if not path.is_file() or path.suffix.lower() not in allowed:
                raise ValueError(f"Unrecognized worker output; reset refused: {path}")
            files.append(path)
    return files


def service_command(user, *args):
    command = ["systemctl"] + (["--user"] if user else []) + list(args)
    if args[0] in {"stop", "start"} and not user:
        command.insert(0, "sudo")
    environment = os.environ.copy()
    environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    environment.setdefault(
        "DBUS_SESSION_BUS_ADDRESS",
        "unix:path=" + environment["XDG_RUNTIME_DIR"] + "/bus",
    )
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=90, env=environment
    )
    if args[0] in {"stop", "start"} and result.returncode:
        raise RuntimeError(
            f"Service operation failed: {command}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def connect(path):
    # mode=rw prevents accidentally creating a DB if a path disappears.
    return sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)


def reset(databases, texts, destination, deletions=()):
    """Back up all targets before any reset; restore all on a reset failure."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    entries = []
    for path, statements in databases.items():
        if not path.exists():
            continue
        with connect(path) as source:
            names = {
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            sql = [
                value
                for name, value in statements.items()
                if name in names
                or (
                    "artifacts" in names
                    and value.startswith("DELETE FROM artifacts WHERE id=")
                )
            ]
            if not sql:
                continue
            backup = destination / f"{len(entries):03d}.sqlite"
            with sqlite3.connect(backup) as target:
                source.backup(target)  # Includes committed WAL data.
            backup.chmod(0o600)
            entries.append(
                {"path": str(path), "backup": backup.name, "kind": "sqlite", "sql": sql}
            )
    for path in texts:
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError(f"Not a regular memory file: {path}")
        backup = destination / f"{len(entries):03d}.text"
        shutil.copyfile(path, backup)
        backup.chmod(0o600)
        entries.append({"path": str(path), "backup": backup.name, "kind": "text"})
    for path in deletions:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Unsafe generated file: {path}")
        backup = destination / f"{len(entries):03d}.generated"
        shutil.copyfile(path, backup)
        backup.chmod(0o600)
        entries.append({"path": str(path), "backup": backup.name, "kind": "delete"})
    manifest = destination / "manifest.json"
    manifest.write_text(
        json.dumps({"status": "backed_up", "entries": entries}, indent=2)
    )
    manifest.chmod(0o600)
    try:
        for entry in entries:
            path = Path(entry["path"])
            if entry["kind"] == "sqlite":
                with connect(path) as db:
                    db.execute("PRAGMA foreign_keys=ON")
                    for statement in entry["sql"]:
                        db.execute(statement)
            elif entry["kind"] == "delete":
                path.unlink()
            else:
                path.write_bytes(b"")
    except Exception:
        # API remains stopped if restoration itself fails.
        restore(destination)
        raise
    manifest.write_text(
        json.dumps({"status": "reset_complete", "entries": entries}, indent=2)
    )
    return entries


def restore(destination):
    manifest = json.loads((destination / "manifest.json").read_text())
    for entry in manifest["entries"]:
        path = Path(entry["path"])
        backup = destination / entry["backup"]
        if entry["kind"] == "sqlite":
            with sqlite3.connect(backup) as source, connect(path) as target:
                source.backup(target)
        else:
            shutil.copyfile(backup, path)
            path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Back up and reset; default is preview"
    )
    parser.add_argument(
        "--keep-conversations",
        action="store_true",
        help="Keep conversation history and managed-agent memory",
    )
    parser.add_argument(
        "--include-profile", action="store_true", help="Also clear USER.md files"
    )
    parser.add_argument(
        "--include-knowledge",
        action="store_true",
        help="Also clear knowledge.db indexed chunks (sync may reimport them)",
    )
    parser.add_argument(
        "--backup-dir", type=Path, help="New directory for the private backup"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip systemd control; you must already have stopped every Jarvis writer",
    )
    parser.add_argument(
        "--restore", type=Path, help="Restore a backup made by this script"
    )
    parser.add_argument(
        "--include-generated",
        action="store_true",
        help="Delete tracked generated artifacts, run history and quarantined images",
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="With --include-generated, also remove uploaded artifacts",
    )
    parser.add_argument(
        "--worker-output-dir",
        type=Path,
        action="append",
        default=[],
        help="With --include-generated, back up/delete a local output(s) directory",
    )
    args = parser.parse_args()
    if (args.all_files or args.worker_output_dir) and not args.include_generated:
        parser.error("--all-files / --worker-output-dir require --include-generated")
    if os.geteuid() == 0:
        parser.error(
            "Run as the OpenJarvis user, not sudo; "
            "service control invokes sudo when needed"
        )
    from openjarvis.core.config import load_config
    from openjarvis.core.paths import get_config_dir

    home = get_config_dir()

    def selected_plan():
        load_config.cache_clear()
        config = load_config()
        databases, texts = plan(
            config,
            home,
            keep_conversations=args.keep_conversations,
            include_knowledge=args.include_knowledge,
            include_profile=args.include_profile,
        )
        deletions = (
            generated_plan(config, home, databases, all_files=args.all_files)
            if args.include_generated
            else []
        )
        deletions += worker_files(args.worker_output_dir)
        return databases, texts, sorted(set(deletions))

    databases, texts, deletions = selected_plan() if not args.restore else ({}, [], [])
    if args.restore:
        print(f"Restore backup: {args.restore.expanduser().resolve()}")
    else:
        print("Reset scope: ALL accounts; learned memory and retrieval documents.")
        for path, statements in databases.items():
            print(f"  Database: {path} (selected tables: {', '.join(statements)})")
        for path in texts:
            print(f"  Clear file if present: {path}")
        for path in deletions:
            print(f"  Delete generated file: {path}")
        if args.include_generated:
            print(
                "Also clears generation-run records and quarantined images. "
                "Worker originals are included only with --worker-output-dir."
            )
    print(
        "Preserved: accounts, credentials, settings, approvals, "
        "saved workflows, SOUL.md."
    )
    if not args.apply:
        print(
            "Preview only. Add --apply to execute; close Jarvis CLI/SDK writers first."
        )
        return
    running = []
    if not args.offline:
        services = [(False, SERVICE)]
        restoring_worker_outputs = bool(
            args.restore
            and any(
                entry["kind"] == "delete" and Path(entry["path"]).suffix != ".blob"
                for entry in json.loads(
                    (args.restore.expanduser() / "manifest.json").read_text()
                )["entries"]
            )
        )
        if args.worker_output_dir or restoring_worker_outputs:
            services += [
                (True, "openjarvis-image.service"),
                (False, "hunyuan3d-worker.service"),
            ]
        for user, unit in services:
            state = service_command(user, "is-active", unit)
            if state not in {"active", "inactive", "failed"}:
                raise RuntimeError(
                    f"Cannot establish {unit} state; stop writers and use --offline"
                )
            if state == "active":
                running.append((user, unit))
            service_command(user, "stop", unit)
            if service_command(user, "is-active", unit) not in {"inactive", "failed"}:
                raise RuntimeError(f"{unit} did not stop; memory was not reset")
    # Keep stopped after any failure so no producer can overwrite backup/rollback.
    if args.restore:
        restore(args.restore.expanduser().resolve())
        print("Backup restored.")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = args.backup_dir or home / "backups" / ("memory-reset-" + stamp)
        # Refresh after stopping producers: shutdown can flush facts or finish a run.
        databases, texts, deletions = selected_plan()
        resolved = destination.expanduser().resolve()
        for directory in args.worker_output_dir:
            if resolved.is_relative_to(directory.expanduser().resolve()):
                raise ValueError("Backup directory cannot be inside worker outputs")
        reset(databases, texts, resolved, deletions)
        print(f"Memory reset. Backup: {destination}")
    for user, unit in reversed(running):
        service_command(user, "start", unit)
    print(
        "Reload every Jarvis console and start a new chat. "
        "Scheduled syncs may repopulate knowledge."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"Reset failed: {exc}. API may be stopped; check before restarting.",
            file=sys.stderr,
        )
        sys.exit(1)
