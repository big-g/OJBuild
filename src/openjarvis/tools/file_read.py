"""File read tool — read file contents with path validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

# Maximum file size to read (1 MB)
_MAX_SIZE_BYTES = 1_048_576


@ToolRegistry.register("file_read")
class FileReadTool(BaseTool):
    """Read file contents with optional directory restrictions."""

    tool_id = "file_read"

    def __init__(
        self,
        allowed_dirs: Optional[List[str]] = None,
    ) -> None:
        self._allowed_dirs = [Path(d).resolve() for d in (allowed_dirs or [])]

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="file_read",
            description=("Read the contents of a file. Returns the text content."),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": ("Max lines to return (default: all)."),
                    },
                },
                "required": ["path"],
            },
            category="filesystem",
            required_capabilities=["file:read"],
        )

    def _is_path_allowed(self, path: Path) -> bool:
        """Check if path is within allowed directories."""
        if not self._allowed_dirs:
            return True
        resolved = path.resolve()
        return any(
            resolved == d or resolved.is_relative_to(d) for d in self._allowed_dirs
        )

    def execute(self, **params: Any) -> ToolResult:
        file_path = params.get("path", "")
        if not file_path:
            return ToolResult(
                tool_name="file_read",
                content="No path provided.",
                success=False,
            )
        path = Path(file_path)
        # Block sensitive files (secrets, credentials, keys)
        from openjarvis.security.file_policy import is_sensitive_file

        if is_sensitive_file(path):
            return ToolResult(
                tool_name="file_read",
                content=f"Access denied: {file_path} is a sensitive file.",
                success=False,
            )
        if not path.exists():
            return ToolResult(
                tool_name="file_read",
                content=f"File not found: {file_path}",
                success=False,
            )
        if not path.is_file():
            return ToolResult(
                tool_name="file_read",
                content=f"Not a file: {file_path}",
                success=False,
            )
        if not self._is_path_allowed(path):
            return ToolResult(
                tool_name="file_read",
                content=f"Access denied: {file_path} is outside allowed directories.",
                success=False,
            )
        # Check size
        try:
            size = path.stat().st_size
        except OSError as exc:
            return ToolResult(
                tool_name="file_read",
                content=f"Could not stat file: {exc}",
                success=False,
            )
        if size > _MAX_SIZE_BYTES:
            return ToolResult(
                tool_name="file_read",
                content=(
                    f"File too large: {file_path} ({size} bytes). "
                    f"Maximum is {_MAX_SIZE_BYTES} bytes."
                ),
                success=False,
            )

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                tool_name="file_read",
                content=f"File is not valid UTF-8 text: {file_path}",
                success=False,
            )
        except OSError as exc:
            return ToolResult(
                tool_name="file_read",
                content=f"Could not read file: {exc}",
                success=False,
            )

        max_lines = params.get("max_lines")
        if max_lines is not None:
            try:
                max_lines = int(max_lines)
            except (TypeError, ValueError):
                return ToolResult(
                    tool_name="file_read",
                    content="max_lines must be an integer.",
                    success=False,
                )
            if max_lines < 1:
                return ToolResult(
                    tool_name="file_read",
                    content="max_lines must be at least 1.",
                    success=False,
                )
            lines = content.splitlines()
            content = "\n".join(lines[:max_lines])

        return ToolResult(
            tool_name="file_read",
            content=content,
            success=True,
            metadata={"path": str(path), "size_bytes": size},
        )


__all__ = ["FileReadTool"]
