"""Bounded public research transport and isolated worker lifecycle."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from openjarvis.core.types import ToolResult
from openjarvis.security.public_http import (
    fetch_public_source,
    normalize_source_url,
    source_origin,
)

USER_AGENT = "OpenJarvis-Sources/1.0"
MAX_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_WORKER_OUTPUT = 256 * 1024
_WORKERS = threading.BoundedSemaphore(2)


class ResearchTransport:
    """GET-only, exact-origin, DNS-pinned HTTP with a shared request budget."""

    def __init__(self, url, *, timeout=50, max_requests=50):
        self.url = normalize_source_url(url)
        self.origin = source_origin(self.url)
        self.deadline = time.monotonic() + timeout
        self.max_requests = max_requests
        self.requests = self.bytes = 0

    def fetch(self, url):
        url = normalize_source_url(url)
        if source_origin(url) != self.origin:
            raise ValueError("Off-origin research request blocked")
        if self.requests >= self.max_requests or time.monotonic() >= self.deadline:
            raise ValueError("Research request or time limit exceeded")
        self.requests += 1
        response = fetch_public_source(
            url,
            accept="*/*",
            allowed_origin=self.origin,
            max_bytes=min(MAX_BYTES, MAX_TOTAL_BYTES - self.bytes),
            deadline=self.deadline,
            follow_redirects=False,
        )
        self.bytes += len(response.content)
        if self.bytes > MAX_TOTAL_BYTES or len(response.content) > MAX_BYTES:
            raise ValueError("Research byte limit exceeded")
        if source_origin(str(response.url)) != self.origin:
            raise ValueError("Research response origin mismatch")
        if response.status_code in {301, 302, 303, 307, 308}:
            target = normalize_source_url(
                urljoin(url, response.headers.get("Location", ""))
            )
            if (
                not response.headers.get("Location")
                or source_origin(target) != self.origin
            ):
                raise ValueError("Unsafe research redirect")
            response.headers["Location"] = target
        return response


def run_worker(module, payload, *, timeout):
    """Kill the entire owned process group on timeout; bound output in a file."""
    if module not in {
        "openjarvis.tools.scrapy_worker",
        "openjarvis.tools.render_worker",
    }:
        raise ValueError("Unknown research worker")
    if not _WORKERS.acquire(blocking=False):
        raise ValueError("Research workers are busy; retry later")
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [sys.executable, "-m", module],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            try:
                process.communicate(json.dumps(payload).encode(), timeout=timeout)
            finally:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.kill()
                process.wait()
            output.seek(0)
            data = output.read(MAX_WORKER_OUTPUT + 1)
            if process.returncode != 0 or len(data) > MAX_WORKER_OUTPUT:
                raise ValueError("Research worker failed or exceeded its output limit")
            result = json.loads(data)
            if not isinstance(result, dict) or result.get("error"):
                raise ValueError("Research worker failed")
            return result
    finally:
        _WORKERS.release()


def page_result(tool_name, provider, url, payload, *, max_pages):
    """Validate worker provenance before making any external evidence available."""
    pages = payload.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= max_pages:
        raise ValueError("Research returned no valid page inventory")
    records, seen = [], set()
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("Invalid research page")
        page_url = normalize_source_url(page.get("url"))
        if source_origin(page_url) != source_origin(url) or page_url in seen:
            raise ValueError("Invalid or repeated research page origin")
        seen.add(page_url)
        title, content = page.get("title", ""), page.get("content")
        if (
            not isinstance(title, str)
            or len(title) > 300
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 4000
        ):
            raise ValueError("Invalid research page text")
        retrieved_at = page.get("retrieved_at")
        parsed = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if parsed.tzinfo is None or not 0 <= (now - parsed).total_seconds() <= 300:
            raise ValueError("Invalid research fetch timestamp")
        truncated = page.get("truncated", False)
        if type(truncated) is not bool:
            raise ValueError("Invalid research truncation flag")
        records.append(
            {
                "title": title or page_url,
                "url": page_url,
                "source_id": page_url,
                "content": content,
                "retrieved_at": retrieved_at,
                "truncated": truncated,
            }
        )
    return ToolResult(
        tool_name=tool_name,
        success=True,
        content="\n\n---\n\n".join(
            f"### {record['title']}\nSource: {record['url']}\n{record['content']}"
            for record in records
        ),
        metadata={
            "provider": provider,
            "start_url": url,
            "pages": len(records),
            "coverage": "bounded_public_page_subset",
            "truncated": any(r["truncated"] for r in records),
            "evidence": {
                "provider": provider,
                "retrieved_at": max(r["retrieved_at"] for r in records),
                "records": records,
            },
        },
    )


class RobotsPolicy:
    """One strict, pinned robots policy for every request in this origin."""

    def __init__(self, transport):
        from urllib.parse import urlsplit
        from urllib.robotparser import RobotFileParser

        parsed = urlsplit(transport.url)
        response = transport.fetch(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        self.parser = RobotFileParser()
        if response.status_code == 200:
            self.parser.parse(response.text.splitlines())
        elif response.status_code == 404:
            self.parser.parse([])
        else:
            raise ValueError("Robots policy unavailable or denies access")

    def allowed(self, url):
        return self.parser.can_fetch(USER_AGENT, url)
