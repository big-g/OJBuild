"""Isolated Playwright rendering; every HTTP request uses pinned transport."""

import json
import socket
import sys
import time
from datetime import datetime, timezone

from openjarvis.tools.research_web import USER_AGENT, ResearchTransport, RobotsPolicy


def render(payload):
    from playwright.sync_api import sync_playwright

    transport = ResearchTransport(payload["url"], timeout=25)
    robots = RobotsPolicy(transport)
    if not robots.allowed(transport.url):
        raise ValueError("Robots policy denies access")
    failed = []
    # Reserve an unlistened loopback port so fallback traffic has no proxy.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as proxy_socket:
        proxy_socket.bind(("127.0.0.1", 0))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                chromium_sandbox=True,
                proxy={"server": f"http://127.0.0.1:{proxy_socket.getsockname()[1]}"},
                args=[
                    "--disable-background-networking",
                    "--disable-quic",
                    "--proxy-bypass-list=<-loopback>",
                    "--js-flags=--max-old-space-size=128",
                    "--disable-features=WebRtcHideLocalIpsWithMdns",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            try:
                context = browser.new_context(
                    service_workers="block",
                    accept_downloads=False,
                    user_agent=USER_AGENT,
                    permissions=[],
                )
                if not hasattr(context, "route_web_socket"):
                    raise ValueError("Playwright 1.48+ is required")
                context.add_init_script("""(() => {
                    for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection',
                                        'WebTransport']) {
                        Object.defineProperty(globalThis, name, {
                            value: undefined, writable: false, configurable: false
                        });
                    }
                })();""")
                context.route_web_socket("**/*", lambda ws: ws.close())
                page = context.new_page()

                def route_request(route):
                    request = route.request
                    try:
                        if request.method != "GET" or request.frame.page != page:
                            route.abort()
                            return
                        # Off-origin resources are deliberately outside coverage.
                        from openjarvis.security.public_http import source_origin

                        if source_origin(request.url) != transport.origin:
                            route.abort()
                            return
                        if not robots.allowed(request.url):
                            raise ValueError("Robots policy denies resource")
                        response = transport.fetch(request.url)
                        if response.status_code in {301, 302, 303, 307, 308}:
                            route.fulfill(
                                status=response.status_code,
                                headers={"Location": response.headers["Location"]},
                                body=b"",
                            )
                            return
                        if response.status_code != 200:
                            raise ValueError("Resource fetch failed")
                        # Cookies and encodings are not replayed into the browser.
                        route.fulfill(
                            status=200,
                            body=response.content,
                            headers={
                                "Content-Type": response.headers.get(
                                    "Content-Type", "application/octet-stream"
                                )
                            },
                        )
                    except Exception:
                        failed.append(True)
                        route.abort()

                context.route("**/*", route_request)
                response = page.goto(transport.url, wait_until="load", timeout=25000)
                if (
                    failed
                    or response is None
                    or response.status != 200
                    or time.monotonic() >= transport.deadline
                ):
                    raise ValueError("Incomplete rendered page")
                from openjarvis.security.public_http import (
                    normalize_source_url,
                    source_origin,
                )

                final_url = normalize_source_url(page.url)
                if source_origin(final_url) != transport.origin:
                    raise ValueError("Page navigation changed origin")
                # Bound DOM extraction before transporting strings out of Chromium.
                extracted = page.evaluate("""() => {
                    const text = (document.body && document.body.innerText) || '';
                    return {content: text.slice(0, 4000), truncated: text.length > 4000,
                            title: document.title.slice(0, 300)};
                }""")
                if failed or time.monotonic() >= transport.deadline:
                    raise ValueError("Incomplete rendered page")
                return {
                    "pages": [
                        {
                            **extracted,
                            "url": final_url,
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ]
                }
            finally:
                browser.close()


def main():
    try:
        payload = json.loads(sys.stdin.read(8193))
        result = render(payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({"error": "Page rendering failed"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
