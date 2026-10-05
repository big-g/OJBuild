"""Create a downloadable artifact for the verified current human user."""

from __future__ import annotations

import json

from openjarvis.artifacts.store import ArtifactError, ArtifactStore
from openjarvis.core.correlation import current_identity
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("artifact_save")
class ArtifactSaveTool(BaseTool):
    tool_id = "artifact_save"

    def __init__(self, store: ArtifactStore | None = None):
        self.store = store

    @property
    def spec(self):
        return ToolSpec(
            name=self.tool_id,
            description=(
                "Save generated content as a downloadable file for the current user. "
                "Use this instead of file_write for user downloads. "
                "Never executes the file. "
                "Return the file id and direct the user to Files "
                "to preview and download it. "
                "Text/script/ASCII STL: encoding utf8. Binary files: encoding base64. "
                "Maximum 20 MiB per file. "
                "Do not claim success unless the tool succeeds."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "filename": {"type": "string"},
                    "content": {"type": "string"},
                    "encoding": {"type": "string", "enum": ["utf8", "base64"]},
                },
                "required": ["filename", "content"],
            },
            category="filesystem",
            required_capabilities=["file:write"],
        )

    def execute(self, **params):
        identity = current_identity()
        if identity is None or not identity.user_id:
            return ToolResult(
                tool_name=self.tool_id,
                content="Authenticated user required",
                success=False,
            )
        if set(params) - {"filename", "content", "encoding"}:
            return ToolResult(
                tool_name=self.tool_id,
                content="Unexpected file parameters",
                success=False,
            )
        try:
            store = self.store
            if store is None:
                from openjarvis.core.config import load_config

                store = ArtifactStore(load_config().security.generated_files_dir)
            row = store.save(
                identity.user_id,
                params.get("filename"),
                params.get("content"),
                params.get("encoding", "utf8"),
            )
        except (ArtifactError, OSError) as exc:
            # File paths and server exceptions must not be exposed in tool results.
            message = (
                str(exc)
                if isinstance(exc, ArtifactError)
                else "File storage unavailable"
            )
            return ToolResult(tool_name=self.tool_id, content=message, success=False)
        return ToolResult(
            tool_name=self.tool_id,
            content=json.dumps({**row, "preview_page": f"/files?file={row['id']}"}),
            metadata={"artifact": row},
            success=True,
        )
