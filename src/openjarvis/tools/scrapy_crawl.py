"""Bounded, same-host website crawling with Scrapy.

Scrapy is an optional dependency. Crawls run in a child Python process so its
Twisted reactor cannot interfere with the long-lived OpenJarvis server.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.security.ssrf import check_ssrf
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_MAX_PAGES = 8
_MAX_CHARS_PER_PAGE = 4000
_CRAWL_TIMEOUT_SECONDS = 60
_USER_AGENT = "OpenJarvis/1.0 (+https://github.com/big-g/OJBuild)"


def _valid_http_url(url: str) -> tuple[str | None, str | None]:
    """Return a normalized URL, or an explanatory validation error."""
    normalized = urldefrag(url.strip())[0]
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return None, "The URL is malformed."
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None, "A full http:// or https:// URL is required."
    if parsed.username or parsed.password:
        return None, "URLs containing embedded credentials are not allowed."
    error = check_ssrf(normalized)
    if error:
        return None, error
    return normalized, None


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


@ToolRegistry.register("web_crawl")
class ScrapyCrawlTool(BaseTool):
    """Fetch a URL and a small number of same-host pages for evidence."""

    tool_id = "web_crawl"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_crawl",
            description=(
                "Crawl a website starting from one URL. Fetches a small, bounded "
                "number of HTML pages on that exact host, obeys robots.txt, and "
                "returns page text with source URLs. Use web_search for queries."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Starting http:// or https:// page URL.",
                    },
                    "max_pages": {
                        "type": "integer",
                        "description": "Page limit from 1 to 8. Defaults to 3.",
                    },
                },
                "required": ["url"],
            },
            category="search",
            timeout_seconds=70,
            required_capabilities=["network:fetch"],
            evidence_kinds=["current", "external"],
            metadata={
                "optional_dependency": "scrapy",
                "install_extra": "tools-crawl",
                "same_host_only": True,
                "robots_txt": True,
            },
        )

    def execute(self, **params: Any) -> ToolResult:
        raw_url = params.get("url", "")
        if not isinstance(raw_url, str) or not raw_url.strip():
            return ToolResult(
                tool_name=self.tool_id,
                content="No URL provided.",
                success=False,
            )

        url, validation_error = _valid_http_url(raw_url)
        if url is None:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Crawl blocked: {validation_error}",
                success=False,
            )

        try:
            import importlib.util

            if importlib.util.find_spec("scrapy") is None:
                return ToolResult(
                    tool_name=self.tool_id,
                    content=(
                        "Scrapy is not installed. Install it with "
                        "`uv sync --extra tools-crawl`."
                    ),
                    success=False,
                )
        except (ImportError, ValueError):
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "Scrapy is not installed. Install it with "
                    "`uv sync --extra tools-crawl`."
                ),
                success=False,
            )

        payload = {
            "url": url,
            "max_pages": _bounded_int(
                params.get("max_pages"), default=3, minimum=1, maximum=_MAX_PAGES
            ),
            "max_chars_per_page": _MAX_CHARS_PER_PAGE,
        }
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "openjarvis.tools.scrapy_worker"],
                input=json.dumps(payload),
                capture_output=True,
                check=False,
                text=True,
                timeout=_CRAWL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_name=self.tool_id,
                content="Website crawl timed out.",
                success=False,
            )
        except OSError as exc:
            logger.warning("Could not start Scrapy worker: %s", exc)
            return ToolResult(
                tool_name=self.tool_id,
                content="Could not start the Scrapy crawl worker.",
                success=False,
            )

        try:
            worker_result = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Scrapy worker returned invalid output (exit=%s): %s",
                completed.returncode,
                completed.stderr[-1000:],
            )
            return ToolResult(
                tool_name=self.tool_id,
                content="Website crawl failed before it returned page data.",
                success=False,
            )

        if completed.returncode != 0 or worker_result.get("error"):
            error = str(worker_result.get("error") or "Website crawl failed.")
            if "No module named 'scrapy'" in error:
                error = (
                    "Scrapy is not installed. Install it with "
                    "`uv sync --extra tools-crawl`."
                )
            return ToolResult(
                tool_name=self.tool_id,
                content=error,
                success=False,
            )

        pages = worker_result.get("pages")
        if not isinstance(pages, list):
            pages = []
        usable_pages = [
            page
            for page in pages
            if isinstance(page, dict)
            and isinstance(page.get("content"), str)
            and page["content"].strip()
            and isinstance(page.get("url"), str)
        ]
        if not usable_pages:
            return ToolResult(
                tool_name=self.tool_id,
                content="The crawl returned no readable HTML pages.",
                success=False,
                metadata={"pages": 0, "start_url": url},
            )

        retrieved_at = datetime.now(timezone.utc).isoformat()
        evidence_records = [
            {
                "title": str(page.get("title") or page["url"]),
                "url": page["url"],
                "source_id": page["url"],
                "content": page["content"],
                "depth": page.get("depth", 0),
            }
            for page in usable_pages
        ]
        content = "\n\n---\n\n".join(
            f"### {record['title']}\nSource: {record['url']}\n{record['content']}"
            for record in evidence_records
        )
        return ToolResult(
            tool_name=self.tool_id,
            content=content,
            success=True,
            metadata={
                "mode": "crawl",
                "provider": "scrapy",
                "start_url": url,
                "pages": len(evidence_records),
                "evidence": {
                    "provider": "scrapy",
                    "retrieved_at": retrieved_at,
                    "records": evidence_records,
                },
            },
        )


def _run_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one isolated, same-host Scrapy crawl and return scraped pages."""
    import scrapy
    from scrapy.crawler import CrawlerProcess
    from scrapy.exceptions import IgnoreRequest
    from scrapy.http import HtmlResponse

    start_url = str(payload["url"])
    start_host = urlsplit(start_url).hostname.lower().rstrip(".")
    max_pages = _bounded_int(
        payload.get("max_pages"), default=3, minimum=1, maximum=_MAX_PAGES
    )
    max_chars = _bounded_int(
        payload.get("max_chars_per_page"),
        default=_MAX_CHARS_PER_PAGE,
        minimum=500,
        maximum=_MAX_CHARS_PER_PAGE,
    )
    pages: list[dict[str, Any]] = []

    class SameHostSSRFGuard:
        """Validate every initial, linked, and redirected request."""

        def process_request(self, request, spider):
            hostname = urlsplit(request.url).hostname
            if not hostname or hostname.lower().rstrip(".") != start_host:
                raise IgnoreRequest("Off-host crawl request blocked.")
            error = check_ssrf(request.url)
            if error:
                raise IgnoreRequest(error)
            return None

    class SameHostSpider(scrapy.Spider):
        name = "openjarvis_bounded_crawl"
        allowed_domains = [start_host]

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._start_url = start_url
            self._max_pages = max_pages
            self._max_chars = max_chars
            self._scheduled = 1

        def start_requests(self):
            yield scrapy.Request(
                self._start_url,
                callback=self.parse,
                errback=self.on_error,
                dont_filter=True,
            )

        async def start(self):
            # Scrapy 2.13+ calls start(); keep start_requests() too for the
            # lower supported versions where start() does not exist.
            yield scrapy.Request(
                self._start_url,
                callback=self.parse,
                errback=self.on_error,
                dont_filter=True,
            )

        def parse(self, response):
            if not isinstance(response, HtmlResponse):
                return

            title = " ".join(response.xpath("//title//text()").getall()).strip()
            text_nodes = response.xpath(
                "//body//text()[not(ancestor::script) and not(ancestor::style) "
                "and not(ancestor::noscript) and not(ancestor::svg)]"
            ).getall()
            content = re.sub(r"\s+", " ", " ".join(text_nodes)).strip()
            if content:
                pages.append(
                    {
                        "title": title[:300],
                        "url": response.url,
                        "content": content[: self._max_chars],
                        "depth": int(response.meta.get("depth", 0)),
                    }
                )

            if self._scheduled >= self._max_pages:
                return
            if int(response.meta.get("depth", 0)) >= 2:
                return

            for href in response.css("a::attr(href)").getall():
                candidate = urldefrag(urljoin(response.url, href))[0]
                parsed = urlsplit(candidate)
                if (
                    parsed.scheme not in {"http", "https"}
                    or parsed.hostname is None
                    or parsed.hostname.lower().rstrip(".") != start_host
                    or parsed.username
                    or parsed.password
                    or not self._crawlable_path(parsed.path)
                ):
                    continue
                if check_ssrf(candidate):
                    continue
                self._scheduled += 1
                yield scrapy.Request(
                    candidate,
                    callback=self.parse,
                    errback=self.on_error,
                )
                if self._scheduled >= self._max_pages:
                    break

        @staticmethod
        def _crawlable_path(path: str) -> bool:
            return not path.lower().endswith(
                (
                    ".7z",
                    ".avi",
                    ".csv",
                    ".doc",
                    ".docx",
                    ".gif",
                    ".gz",
                    ".jpeg",
                    ".jpg",
                    ".mp3",
                    ".mp4",
                    ".pdf",
                    ".png",
                    ".ppt",
                    ".pptx",
                    ".rar",
                    ".svg",
                    ".tar",
                    ".webp",
                    ".xls",
                    ".xlsx",
                    ".zip",
                )
            )

        def on_error(self, failure):
            self.logger.debug("Crawl request failed: %s", failure.getErrorMessage())

    settings = {
        "AUTOTHROTTLE_ENABLED": True,
        "CONCURRENT_REQUESTS": 2,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": 0.25,
        "DOWNLOAD_MAXSIZE": 2_000_000,
        "DOWNLOAD_TIMEOUT": 15,
        "LOG_ENABLED": False,
        "REDIRECT_MAX_TIMES": 3,
        "ROBOTSTXT_OBEY": True,
        "LOG_INSTALL_ROOT_HANDLER": False,
        "TELNETCONSOLE_ENABLED": False,
        "USER_AGENT": _USER_AGENT,
        "DOWNLOADER_MIDDLEWARES": {SameHostSSRFGuard: 50},
    }
    process = CrawlerProcess(settings=settings)
    crawler = process.create_crawler(SameHostSpider)
    process.crawl(crawler)
    process.start(install_signal_handlers=False)
    return {"pages": pages}


def _worker_main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = _run_worker(payload)
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:  # noqa: BLE001
        json.dump({"error": f"Scrapy crawl failed: {exc}"}, sys.stdout)
        sys.stdout.write("\n")
        return 1


__all__ = ["ScrapyCrawlTool"]
