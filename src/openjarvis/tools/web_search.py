"""Web search with optional Tavily and explicit keyless provider fallback."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.security.public_http import fetch_public_source
from openjarvis.security.ssrf import check_ssrf
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_KEYLESS_BACKENDS = ("duckduckgo", "bing", "brave")


class _PageText(HTMLParser):
    """Extract visible text and preserve paragraph boundaries for forecasts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1
        elif not self.hidden and tag in {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return "\n".join(
            line
            for raw in "".join(self.parts).splitlines()
            if (line := " ".join(raw.split()))
        )


@ToolRegistry.register("web_search")
class WebSearchTool(BaseTool):
    """Search public web pages; no API key is required."""

    tool_id = "web_search"
    is_local = False

    def __init__(self, api_key: str | None = None, max_results: int = 5):
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY")
        self._max_results = max_results

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_search",
            description=(
                "Search the web for current information."
                " No API key required. Reads leading keyless results for details."
                " Pass a source URL as query to read that page directly."
                " For US weather, prefer site:forecast.weather.gov searches"
                " and read the forecast page; check its validity dates."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return.",
                    },
                    "fetch_pages": {
                        "type": "boolean",
                        "description": (
                            "Read up to two keyless result pages (default true)."
                        ),
                    },
                },
                "required": ["query"],
            },
            category="search",
            timeout_seconds=75,
            required_capabilities=["network:fetch"],
            evidence_kinds=["current", "external"],
            metadata={
                "optional_api_key": "TAVILY_API_KEY",
                "fallback": "duckduckgo,bing,brave",
            },
        )

    @staticmethod
    def _is_url(text: str) -> bool:
        """Check if text is a URL."""
        stripped = text.strip()
        return stripped.startswith("http://") or stripped.startswith("https://")

    @staticmethod
    def _extract_url(text: str) -> str | None:
        """Extract the first URL from text, if any."""
        import re as _re

        match = _re.search(r"https?://[^\s,;\"'<>]+", text)
        return match.group(0).rstrip(".,;)") if match else None

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Convert known PDF URLs to their HTML equivalents."""
        import re as _re

        # arxiv: /pdf/ID → /abs/ID (abstract page with full metadata)
        m = _re.match(r"(https?://arxiv\.org)/pdf/(.+?)(?:\.pdf)?$", url)
        if m:
            return f"{m.group(1)}/abs/{m.group(2)}"
        return url

    @staticmethod
    def _fetch_url(
        url: str, max_chars: int = 12000, *, deadline: float | None = None
    ) -> str:
        """Fetch a URL and return extracted text content."""
        url = WebSearchTool._normalize_url(url)
        ssrf_error = check_ssrf(url)
        if ssrf_error:
            raise ValueError(ssrf_error)
        resp = fetch_public_source(
            url.strip(),
            accept="text/html,text/plain;q=0.9",
            max_bytes=2 * 1024 * 1024,
            deadline=deadline or time.monotonic() + 20,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "application/pdf" in content_type:
            return (
                "[This URL points to a PDF file which"
                f" cannot be read directly. URL: {url}]"
            )
        if "html" in content_type:
            parser = _PageText()
            parser.feed(resp.text)
            text = parser.text()
        elif "text/plain" in content_type:
            text = resp.text.strip()
        else:
            raise ValueError("Source did not return readable HTML or plain text")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[Content truncated]"
        return text

    def _duckduckgo_search(
        self, query: str, max_results: int, *, backend: str = "duckduckgo"
    ) -> tuple[str, list[dict[str, str]]]:
        """Search one named provider, never DDGS's randomized auto backends."""
        from ddgs import DDGS

        ddgs = DDGS(timeout=8)
        raw_results = list(ddgs.text(query, max_results=max_results, backend=backend))

        formatted_results = []
        provenance_results: list[dict[str, str]] = []

        for r in raw_results:
            if not isinstance(r, dict):
                continue
            title = r.get("title", "Untitled")
            url = r.get("href", "")
            snippet = r.get("body", "")
            if not isinstance(url, str) or not isinstance(snippet, str):
                continue
            if not url.startswith(("https://", "http://")) or not snippet.strip():
                continue

            formatted_results.append(f"### {title}\nSource: {url}\nSummary: {snippet}")

            provenance_results.append(
                {
                    "title": title,
                    "url": url,
                    "content": snippet,
                }
            )

        formatted = "\n\n---\n\n".join(formatted_results)
        return formatted, provenance_results

    def _read_result_pages(self, records, attempts):
        """Keep snippets if a bounded public page read fails; never invent text."""
        deadline = time.monotonic() + 16
        for record in records[:2]:
            if time.monotonic() >= deadline:
                break
            try:
                content = self._fetch_url(record["url"], deadline=deadline)
                if not content or "cannot be read directly" in content:
                    raise ValueError("No readable source text")
                record["content"] = content
                record["content_scope"] = "page_text"
                record["retrieved_at"] = datetime.now(timezone.utc).isoformat()
                attempts.append({"provider": "web_fetch", "status": "ok"})
            except Exception as exc:
                attempts.append(
                    {
                        "provider": "web_fetch",
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                )
                logger.info("Search page read failed (%s)", type(exc).__name__)

    def execute(self, **params: Any) -> ToolResult:
        query = params.get("query", "")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                tool_name="web_search",
                content="No query provided.",
                success=False,
            )

        # If the query contains a URL, fetch it directly instead of searching
        url = self._extract_url(query) if not self._is_url(query) else query.strip()
        if url:
            try:
                content = self._fetch_url(url)
                evidence_records = (
                    [{"url": url, "content": content}]
                    if content and "cannot be read directly" not in content
                    else []
                )
                return ToolResult(
                    tool_name="web_search",
                    content=content or "No content found at URL.",
                    success=True,
                    metadata={
                        "url": url,
                        "mode": "fetch",
                        "evidence": {
                            "provider": "web_fetch",
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "records": evidence_records,
                        },
                    },
                )
            except Exception as exc:
                return ToolResult(
                    tool_name="web_search",
                    content=f"Failed to fetch URL: {exc}",
                    success=False,
                )

        max_results = params.get("max_results", self._max_results)
        fetch_pages = params.get("fetch_pages", True)
        if (
            type(max_results) is not int
            or not 1 <= max_results <= 10
            or type(fetch_pages) is not bool
        ):
            return ToolResult(
                tool_name="web_search",
                success=False,
                content="max_results must be 1–10 and fetch_pages must be a boolean.",
            )
        attempts: list[dict[str, Any]] = []

        try:
            if not self._api_key:
                raise ImportError("Tavily is optional; no API key configured")
            from tavily import TavilyClient

            client = TavilyClient(api_key=self._api_key, timeout=8)
            response = client.search(
                query,
                max_results=max_results,
                search_depth="advanced",
                include_usage=True,
            )

            results = response.get("results", [])
            if not results:
                attempts.append({"provider": "tavily", "status": "empty"})
                raise ValueError("Tavily returned no results")
            formatted_parts = []
            provenance_results = []

            for r in results:
                if not isinstance(r, dict):
                    continue
                title = r.get("title", "Untitled")
                url = r.get("url", "")
                content = r.get("content", "") or r.get("snippet", "")
                if not isinstance(url, str) or not isinstance(content, str):
                    continue
                if not url.startswith(("https://", "http://")) or not content.strip():
                    continue

                formatted_parts.append(
                    f"### {title}\nSource: {url}\nSummary: {content}"
                )

                provenance_results.append(
                    {
                        "title": title,
                        "url": url,
                        "content": content,
                    }
                )

            if not provenance_results:
                attempts.append({"provider": "tavily", "status": "empty"})
                raise ValueError("Tavily returned no usable results")
            formatted = "\n\n---\n\n".join(formatted_parts)
            logger.info(
                "Web search provider=tavily results=%d", len(provenance_results)
            )
            return ToolResult(
                tool_name="web_search",
                content=formatted or "No results found.",
                success=True,
                metadata={
                    "num_results": len(provenance_results),
                    "engine": "tavily",
                    "attempts": [
                        {
                            "provider": "tavily",
                            "status": "ok",
                            "num_results": len(provenance_results),
                        }
                    ],
                    "credits": (response.get("usage") or {}).get("credits"),
                    "results": provenance_results,
                    "evidence": {
                        "provider": "tavily",
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "records": provenance_results,
                    },
                },
            )
        except Exception as exc:
            if self._api_key and not attempts:
                attempts.append(
                    {
                        "provider": "tavily",
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                )
            logger.debug(
                "Tavily error (%s), falling back to DuckDuckGo", type(exc).__name__
            )

        for backend in _KEYLESS_BACKENDS:
            try:
                formatted, provenance_results = self._duckduckgo_search(
                    query, max_results, backend=backend
                )
                attempts.append(
                    {
                        "provider": backend,
                        "status": "ok" if provenance_results else "empty",
                        "num_results": len(provenance_results),
                    }
                )
                if not provenance_results:
                    continue
                if fetch_pages:
                    self._read_result_pages(provenance_results, attempts)
                    formatted = "\n\n---\n\n".join(
                        f"### {r['title']}\nSource: {r['url']}\nSummary: {r['content']}"
                        for r in provenance_results
                    )
                logger.info(
                    "Web search provider=%s results=%d",
                    backend,
                    len(provenance_results),
                )
                return ToolResult(
                    tool_name="web_search",
                    content=formatted or "No results found.",
                    success=True,
                    metadata={
                        "engine": backend,
                        "attempts": attempts,
                        "num_results": len(provenance_results),
                        "results": provenance_results,
                        "evidence": {
                            "provider": backend,
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "records": provenance_results,
                        },
                    },
                )

            except Exception as exc:
                attempts.append(
                    {
                        "provider": backend,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                )
                logger.warning(
                    "Web search provider=%s failed (%s)", backend, type(exc).__name__
                )
                if isinstance(exc, ImportError):
                    break
        return ToolResult(
            tool_name="web_search",
            success=False,
            content=(
                "Web search returned no usable results. "
                "Inspect provider attempts in metadata."
            ),
            metadata={
                "attempts": attempts,
                "evidence": {"records": []},
            },
        )


__all__ = ["WebSearchTool"]
