"""General local-document source with persisted, explicitly selected scope."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.config import DEFAULT_CONFIG_DIR
from openjarvis.core.registry import ConnectorRegistry
from openjarvis.security.file_utils import secure_write_json

_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".html",
    ".htm",
    ".pdf",
}
_SKIP_DIRS = {"node_modules", "__pycache__"}
_MAX_BYTES = 8 * 1024 * 1024
_MAX_FILES = 2000


def _open_scoped(root: Path, relative: Path) -> int:
    """Open every directory component without following replacement symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root, flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
    finally:
        os.close(directory)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _extract(data: bytes, suffix: str) -> str:
    if suffix == ".pdf":
        try:
            import pdfplumber
        except ImportError as exc:
            raise RuntimeError("PDF ingestion requires the memory-pdf extra") from exc
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > 200:
                raise ValueError("PDF exceeds the 200-page ingestion limit")
            pages = [page.extract_text() or "" for page in pdf.pages]
            if not any(text.strip() for text in pages):
                raise ValueError("PDF contains no extractable text; OCR is required")
            return "\n\n".join(f"Page {i}\n{text}" for i, text in enumerate(pages, 1))
    text = data.decode("utf-8-sig")
    if suffix in {".html", ".htm"}:
        parser = _HTMLText()
        parser.feed(text)
        return "".join(parser.parts).strip()
    return text


@ConnectorRegistry.register("local_files")
class LocalFilesConnector(BaseConnector):
    connector_id = "local_files"
    display_name = "Local Files"
    auth_type = "filesystem"
    required_capabilities = ("connector:local_files:read", "file:read")

    def __init__(self, *, config_path: str = "", root_path: str = "") -> None:
        self._configured_root = root_path
        self._config_path = (
            Path(config_path)
            if config_path
            else (DEFAULT_CONFIG_DIR / "connectors" / "local_files.json")
        )
        self._status = SyncStatus()

    def _root(self) -> Path:
        path = self._configured_root
        if not path:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            path = data["path"]
        root = Path(path).expanduser().resolve(strict=True)
        if str(root) != path:
            raise ValueError(
                "Configured directory changed; test and reconfigure the source"
            )
        if not root.is_dir():
            raise ValueError("Local Files requires a directory on the server")
        return root

    def configure_path(self, path: str) -> None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("A directory path on the server is required")
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Local Files requires a directory on the server")
        if self._config_path.exists():
            previous = json.loads(self._config_path.read_text(encoding="utf-8"))["path"]
            if previous != str(root):
                raise ValueError("Disconnect Local Files before changing its directory")
        secure_write_json(self._config_path, {"path": str(root)})

    def is_connected(self) -> bool:
        try:
            self._root()
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def disconnect(self) -> None:
        self._config_path.unlink(missing_ok=True)
        self._status = SyncStatus()

    def sync(self, *, since=None, cursor=None) -> Iterator[Document]:
        root = self._root()
        paths = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".")
                and d not in _SKIP_DIRS
                and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(files):
                path = Path(directory) / name
                if name.startswith(".") or path.suffix.lower() not in _EXTENSIONS:
                    continue
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    continue
                paths.append(path)
                if len(paths) > _MAX_FILES:
                    raise ValueError(
                        "Local Files exceeds the 2000-file ingestion limit"
                    )
        self._status = SyncStatus(state="syncing", items_total=len(paths))
        root_id = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        try:
            for path in paths:
                self.check_sync_cancelled()
                descriptor = _open_scoped(root, path.relative_to(root))
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    modified = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
                    if since is not None:
                        cutoff = (
                            since
                            if since.tzinfo
                            else since.replace(tzinfo=timezone.utc)
                        )
                        if modified < cutoff:
                            continue
                    data = stream.read(_MAX_BYTES + 1)
                if len(data) > _MAX_BYTES:
                    raise ValueError(
                        f"File exceeds the 8 MiB ingestion limit: {path.name}"
                    )
                relative = path.relative_to(root).as_posix()
                source_id = f"{root_id}:{relative}"
                text = _extract(data, path.suffix.lower())
                yield Document(
                    doc_id=f"local_files:{source_id}",
                    source_id=source_id,
                    source=self.connector_id,
                    doc_type="document",
                    content=text,
                    title=relative,
                    url=path.as_uri(),
                    timestamp=modified,
                    metadata={
                        "trust": "auto",
                        "path": relative,
                        "format": path.suffix.lower().lstrip("."),
                        "version": hashlib.sha256(data).hexdigest(),
                    },
                )
                self._status.items_synced += 1
            self._status.state = "idle"
            self._status.last_sync = datetime.now(timezone.utc)
        except Exception as exc:
            self._status.state = "error"
            self._status.error = str(exc)
            raise

    def sync_status(self) -> SyncStatus:
        return self._status
