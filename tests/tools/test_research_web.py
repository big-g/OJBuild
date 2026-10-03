"""Offline pinned research and rendering contracts; no browser binary needed."""

import socket
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from openjarvis.tools import render_worker, research_web, web_render

URL = "https://example.com/page"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(
            socket, name, Mock(side_effect=AssertionError("No networking"))
        )


def response(url=URL, *, status=200, content=b"Page", headers=None):
    return httpx.Response(
        status,
        content=content,
        headers=headers or {"Content-Type": "text/html"},
        request=httpx.Request("GET", url),
    )


def page(**changes):
    return {
        "url": URL,
        "title": "Evidence",
        "content": "Actual evidence",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        **changes,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "https://example.com/?token=secret",
        "https://user:pass@example.com/",
        "file:///tmp/file",
        "https://example.com:8000/",
    ],
)
def test_blocked_targets_do_not_start_renderer(monkeypatch, url):
    worker = Mock(side_effect=AssertionError("No worker"))
    monkeypatch.setattr(web_render, "run_worker", worker)
    result = web_render.WebRenderTool().execute(url=url)
    assert not result.success and not result.metadata.get("evidence")
    worker.assert_not_called()


def test_render_evidence_contract(monkeypatch):
    monkeypatch.setattr(web_render.importlib.util, "find_spec", lambda name: object())
    worker = Mock(return_value={"pages": [page()]})
    monkeypatch.setattr(web_render, "run_worker", worker)
    result = web_render.WebRenderTool().execute(url=URL)
    assert result.success
    assert result.metadata["evidence"]["records"][0]["url"] == URL
    assert set(web_render.WebRenderTool().spec.required_capabilities) == {
        "network:fetch",
        "code:execute",
    }
    worker.assert_called_once_with(
        "openjarvis.tools.render_worker", {"url": URL}, timeout=35
    )


@pytest.mark.parametrize(
    "pages",
    [
        [],
        [page(url="https://elsewhere.test/")],
        [page(content="")],
        [page(content="x" * 4001)],
        [page(retrieved_at="2020-01-01T00:00:00Z")],
        [page(retrieved_at=None)],
        [page(), page()],
        [page(url="https://example.com/?api_key=reflected")],
    ],
)
def test_invalid_worker_pages_never_become_evidence(monkeypatch, pages):
    monkeypatch.setattr(web_render.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        web_render, "run_worker", lambda *args, **kwargs: {"pages": pages}
    )
    result = web_render.WebRenderTool().execute(url=URL)
    assert not result.success and not result.metadata.get("evidence")


def test_missing_dependency_and_worker_errors(monkeypatch):
    monkeypatch.setattr(web_render.importlib.util, "find_spec", lambda name: None)
    assert (
        "uv sync --extra browser" in web_render.WebRenderTool().execute(url=URL).content
    )
    monkeypatch.setattr(web_render.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        web_render,
        "run_worker",
        Mock(side_effect=ValueError("remote reflected secret")),
    )
    result = web_render.WebRenderTool().execute(url=URL)
    assert not result.success and "remote reflected secret" not in result.content


def test_transport_never_uses_engine_http(monkeypatch):
    fetch = Mock(return_value=response())
    monkeypatch.setattr(research_web, "fetch_public_source", fetch)
    transport = research_web.ResearchTransport(URL, max_requests=1)
    transport.fetch(URL)
    assert fetch.call_args.kwargs["follow_redirects"] is False
    assert fetch.call_args.kwargs["allowed_origin"] == "https://example.com:443"
    assert fetch.call_args.kwargs["max_bytes"] == research_web.MAX_BYTES
    with pytest.raises(ValueError):
        transport.fetch(URL)
    fetch.assert_called_once()


@pytest.mark.parametrize(
    "target",
    ["https://other.test/", "http://example.com/", "https://example.com/?key=secret"],
)
def test_transport_origin_and_query_guard_before_fetch(monkeypatch, target):
    fetch = Mock(side_effect=AssertionError("No HTTP"))
    monkeypatch.setattr(research_web, "fetch_public_source", fetch)
    with pytest.raises(ValueError):
        research_web.ResearchTransport(URL).fetch(target)
    fetch.assert_not_called()


@pytest.mark.parametrize(
    "location",
    [
        "https://elsewhere.test/",
        "http://127.0.0.1/",
        "https://example.com/?token=secret",
        "",
    ],
)
def test_unsafe_redirect_has_no_followup(monkeypatch, location):
    fetch = Mock(return_value=response(status=302, headers={"Location": location}))
    monkeypatch.setattr(research_web, "fetch_public_source", fetch)
    with pytest.raises(ValueError):
        research_web.ResearchTransport(URL).fetch(URL)
    fetch.assert_called_once()


def test_transport_bytes_and_deadline(monkeypatch):
    transport = research_web.ResearchTransport(URL)
    transport.bytes = research_web.MAX_TOTAL_BYTES
    monkeypatch.setattr(
        research_web,
        "fetch_public_source",
        lambda *args, **kwargs: response(content=b"x"),
    )
    with pytest.raises(ValueError):
        transport.fetch(URL)
    transport.deadline = 0
    with pytest.raises(ValueError):
        transport.fetch(URL)


@pytest.mark.parametrize("status", [301, 401, 403, 500])
def test_robots_unavailable_fails_closed(status):
    transport = SimpleNamespace(
        url=URL, fetch=Mock(return_value=response(status=status))
    )
    with pytest.raises(ValueError):
        research_web.RobotsPolicy(transport)


def test_robots_rules():
    transport = SimpleNamespace(
        url=URL,
        fetch=Mock(
            return_value=response(content=b"User-agent: *\nDisallow: /private\n")
        ),
    )
    robots = research_web.RobotsPolicy(transport)
    assert robots.allowed(URL)
    assert not robots.allowed("https://example.com/private")


class Browser:
    def __init__(self, requests):
        self.requests = requests
        self.context = Mock()
        self.context.new_page.return_value = self.page = Mock()
        self.page.url = URL
        self.page.evaluate.return_value = {
            "title": "Rendered",
            "content": "DOM evidence",
            "truncated": False,
        }
        self.routes = []
        self.page.goto.side_effect = self.goto

    def new_context(self, **kwargs):
        self.options = kwargs
        return self.context

    def goto(self, *args, **kwargs):
        handler = self.context.route.call_args.args[1]
        for method, url in self.requests:
            route = Mock(
                request=SimpleNamespace(
                    method=method, url=url, frame=SimpleNamespace(page=self.page)
                )
            )
            handler(route)
            self.routes.append(route)
        return SimpleNamespace(status=200)

    def close(self):
        self.closed = True


def browser_runtime(monkeypatch, requests):
    browser = Browser(requests)
    launch = Mock(return_value=browser)
    runtime = Mock()
    runtime.__enter__ = Mock(
        return_value=SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    )
    runtime.__exit__ = Mock(return_value=False)
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: runtime),
    )
    return browser, launch


def test_renderer_is_ephemeral_and_blocks_non_get_cross_origin(monkeypatch):
    browser, launch = browser_runtime(
        monkeypatch, [("GET", URL), ("POST", URL), ("GET", "https://other.test/")]
    )
    fetch = Mock(
        side_effect=lambda url, **kwargs: response(
            url,
            content=b"User-agent: *\nAllow: /\n"
            if url.endswith("robots.txt")
            else b"HTML",
        )
    )
    monkeypatch.setattr(research_web, "fetch_public_source", fetch)
    result = render_worker.render({"url": URL})
    assert result["pages"][0]["content"] == "DOM evidence" and browser.closed
    assert launch.call_args.kwargs["chromium_sandbox"] is True
    assert browser.options["service_workers"] == "block"
    assert browser.options["accept_downloads"] is False
    assert browser.options["permissions"] == []
    assert len(fetch.call_args_list) == 2
    browser.routes[0].fulfill.assert_called_once()
    browser.routes[1].abort.assert_called_once()
    browser.routes[2].abort.assert_called_once()
    ws = Mock()
    browser.context.route_web_socket.call_args.args[1](ws)
    ws.close.assert_called_once()


def test_renderer_fetch_failure_discards_dom_and_closes(monkeypatch):
    browser, _ = browser_runtime(monkeypatch, [("GET", URL)])
    monkeypatch.setattr(
        research_web,
        "fetch_public_source",
        Mock(
            side_effect=[
                response(content=b"User-agent: *\nAllow: /"),
                ValueError("TLS failed"),
            ]
        ),
    )
    with pytest.raises(ValueError):
        render_worker.render({"url": URL})
    assert browser.closed
    browser.page.evaluate.assert_not_called()


def test_worker_timeout_kills_owned_process_group(monkeypatch):
    process = Mock(
        pid=123456, communicate=Mock(side_effect=subprocess.TimeoutExpired("worker", 1))
    )
    popen = Mock(return_value=process)
    kill = Mock()
    monkeypatch.setattr(research_web.subprocess, "Popen", popen)
    monkeypatch.setattr(research_web.os, "killpg", kill)
    with pytest.raises(subprocess.TimeoutExpired):
        research_web.run_worker(
            "openjarvis.tools.render_worker", {"url": URL}, timeout=1
        )
    kill.assert_called_once_with(123456, research_web.signal.SIGKILL)
    process.wait.assert_called_once()
    assert popen.call_args.kwargs["start_new_session"] is True


def test_worker_output_limit_and_backpressure(monkeypatch):
    def popen(*args, **kwargs):
        kwargs["stdout"].write(b"x" * (research_web.MAX_WORKER_OUTPUT + 1))
        return Mock(returncode=0, pid=123456)

    monkeypatch.setattr(research_web.subprocess, "Popen", popen)
    monkeypatch.setattr(research_web.os, "killpg", Mock())
    with pytest.raises(ValueError):
        research_web.run_worker(
            "openjarvis.tools.render_worker", {"url": URL}, timeout=1
        )
    assert research_web._WORKERS.acquire(False)
    assert research_web._WORKERS.acquire(False)
    try:
        with pytest.raises(ValueError, match="busy"):
            research_web.run_worker(
                "openjarvis.tools.render_worker", {"url": URL}, timeout=1
            )
    finally:
        research_web._WORKERS.release()
        research_web._WORKERS.release()


@pytest.mark.parametrize("failure", [False, True])
def test_scrapy_worker_uses_only_pinned_middleware_and_stages_failures(
    monkeypatch, failure
):
    from openjarvis.tools.scrapy_crawl import _run_worker

    class Request:
        def __init__(self, url, **kwargs):
            self.url, self.method, self.meta = url, "GET", {}

    class Response:
        def __init__(
            self, url, *, body=b"", status=200, headers=None, request=None, **kwargs
        ):
            self.url, self.body, self.status, self.headers = (
                url,
                body,
                status,
                headers or {},
            )
            self.meta = request.meta

        def xpath(self, selector):
            return SimpleNamespace(
                getall=lambda: (
                    ["Title"] if "title" in selector else [self.body.decode()]
                )
            )

        def css(self, selector):
            return SimpleNamespace(getall=lambda: ["/next"] if self.url == URL else [])

    class HtmlResponse(Response):
        pass

    class Spider:
        def __init__(self, **kwargs):
            pass

    captured = {}

    class Process:
        def __init__(self, settings):
            captured.update(settings)

        def create_crawler(self, spider):
            return spider()

        def crawl(self, crawler):
            self.spider = crawler

        def start(self, **kwargs):
            middleware = [
                cls()
                for cls in captured["DOWNLOADER_MIDDLEWARES"]
                if isinstance(cls, type)
            ]
            queue = list(self.spider.start_requests())
            while queue:
                request = queue.pop(0)
                try:
                    for guard in middleware:
                        result = guard.process_request(request, self.spider)
                        if result is not None:
                            break
                    queue.extend(self.spider.parse(result))
                except Exception:
                    self.spider.on_error(None)

    monkeypatch.setitem(
        sys.modules, "scrapy", SimpleNamespace(Spider=Spider, Request=Request)
    )
    monkeypatch.setitem(
        sys.modules, "scrapy.crawler", SimpleNamespace(CrawlerProcess=Process)
    )
    monkeypatch.setitem(
        sys.modules, "scrapy.exceptions", SimpleNamespace(IgnoreRequest=ValueError)
    )
    monkeypatch.setitem(
        sys.modules,
        "scrapy.http",
        SimpleNamespace(Response=Response, HtmlResponse=HtmlResponse),
    )
    calls = []

    def pinned(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("robots.txt"):
            return response(
                url,
                content=b"User-agent: *\nAllow: /\n",
                headers={"Content-Type": "text/plain"},
            )
        if url.endswith("next") and failure:
            raise ValueError("TLS failed")
        return response(url)

    monkeypatch.setattr(research_web, "fetch_public_source", pinned)
    if failure:
        with pytest.raises(ValueError, match="Incomplete crawl"):
            _run_worker({"url": URL, "max_pages": 2})
    else:
        result = _run_worker({"url": URL, "max_pages": 2})
        assert len(result["pages"]) == 2
        assert result["pages"][0]["retrieved_at"]
    assert captured["DOWNLOAD_HANDLERS"] == {"http": None, "https": None}
    assert captured["COOKIES_ENABLED"] is False
    assert captured["RETRY_ENABLED"] is False
    assert len(calls) == 3
    assert all(kwargs["follow_redirects"] is False for _, kwargs in calls)
