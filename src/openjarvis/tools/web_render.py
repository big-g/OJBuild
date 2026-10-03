"""Ephemeral public-page rendering through governed Playwright workers."""

import importlib.util
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.security.public_http import normalize_source_url
from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.tools.research_web import page_result, run_worker


@ToolRegistry.register("web_render")
class WebRenderTool(BaseTool):
    tool_id = "web_render"
    is_local = False

    @property
    def spec(self):
        return ToolSpec(
            name=self.tool_id,
            description=(
                "Render one public web page with JavaScript in an isolated "
                "browser and return bounded text evidence. Same-origin GET "
                "resources only; no login or interactions."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
                "additionalProperties": False,
            },
            category="search",
            timeout_seconds=45,
            required_capabilities=["network:fetch", "code:execute"],
            evidence_kinds=["current", "external"],
            metadata={
                "optional_dependency": "playwright",
                "install_extra": "browser",
                "same_origin_only": True,
                "ephemeral_context": True,
            },
        )

    def execute(self, **params: Any):
        try:
            url = normalize_source_url(params.get("url"))
        except Exception:
            return ToolResult(
                tool_name=self.tool_id,
                content="A public http(s) URL without credentials is required.",
                success=False,
            )
        if importlib.util.find_spec("playwright") is None:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "Playwright is not installed. Use uv sync --extra browser "
                    "and uv run playwright install chromium."
                ),
                success=False,
            )
        try:
            payload = run_worker(
                "openjarvis.tools.render_worker", {"url": url}, timeout=35
            )
            return page_result(self.tool_id, "playwright", url, payload, max_pages=1)
        except Exception:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "Page rendering failed or exceeded its limits; no page "
                    "evidence was returned. Check Chromium installation "
                    "and sandbox support."
                ),
                success=False,
            )
