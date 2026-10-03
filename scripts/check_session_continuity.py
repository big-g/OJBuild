#!/usr/bin/env python3
"""Validate conversation/trace continuity against the existing OpenJarvis service."""

from __future__ import annotations

import argparse
import getpass
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect


class CheckFailed(RuntimeError):
    """A safe diagnostic that contains no credentials or conversation content."""


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    require(
        response.is_success,
        f"{method} {path.split('?')[0]}: HTTP {response.status_code}",
    )
    return response


def check_identity(identity, user_id, session_id):
    require(isinstance(identity, dict), "Missing correlation metadata")
    require(identity.get("user_id") == user_id, "Authenticated user mismatch")
    require(identity.get("session_id") == session_id, "Session identity mismatch")
    require(
        identity.get("conversation_id") == session_id, "Conversation identity mismatch"
    )
    for key in ("request_id", "turn_id", "trace_id"):
        require(bool(identity.get(key)), f"Missing {key}")


def ws_turn(ws, session_id, message, timeout, model):
    ws.send(json.dumps({"message": message, "session_id": session_id, "model": model}))
    deadline = time.monotonic() + timeout
    chunks, identity = [], None
    for _ in range(10000):
        remaining = deadline - time.monotonic()
        require(remaining > 0, "WebSocket turn timed out")
        frame = json.loads(ws.recv(timeout=remaining))
        require(frame.get("type") != "error", "WebSocket returned an error")
        identity = identity or frame.get("correlation")
        require(frame.get("correlation") == identity, "Frame identities differ")
        if frame.get("type") == "chunk":
            chunks.append(frame.get("content", ""))
        elif frame.get("type") == "done":
            require(frame.get("content") == "".join(chunks), "Stream assembly mismatch")
            return frame["content"], identity
    raise CheckFailed("WebSocket frame limit exceeded")


def run_checks(client, connect_ws, *, project_id, model, timeout, report):
    """Use an authenticated disposable login; retain only the new test conversation."""
    user_id = request(client, "GET", "/v1/auth/me").json()["user_id"]
    session = request(
        client,
        "POST",
        "/v1/sessions",
        json={
            "project_id": project_id,
            "title": "Phase 2 acceptance "
            + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
    ).json()
    session_id = session["session_id"]
    report["session_id"] = session_id
    expected, identities = [], []
    prompts = [
        "Reply briefly: HTTP acceptance check.",
        "Reply briefly: WebSocket acceptance check.",
        "Reply briefly: reconnected acceptance check.",
    ]
    response = request(
        client,
        "POST",
        "/v1/chat/completions",
        json={
            "model": model,
            "session_id": session_id,
            "messages": [{"role": "user", "content": prompts[0]}],
        },
    )
    answer = response.json()["choices"][0]["message"]["content"]
    history = request(client, "GET", f"/v1/sessions/{session_id}").json()["messages"]
    require(len(history) == 2, "HTTP exchange was not persisted")
    identity = history[0].get("metadata", {}).get("correlation")
    check_identity(identity, user_id, session_id)
    for key in ("request_id", "turn_id", "trace_id"):
        require(
            response.headers.get("x-" + key.replace("_", "-")) == identity[key],
            "HTTP header/message identity mismatch",
        )
    expected.extend([("user", prompts[0]), ("assistant", answer)])
    identities.append(identity)
    report["checks"].append("HTTP conversation and identity")

    for prompt in prompts[1:]:
        with connect_ws() as ws:
            answer, identity = ws_turn(ws, session_id, prompt, timeout, model)
            check_identity(identity, user_id, session_id)
            expected.extend([("user", prompt), ("assistant", answer)])
            identities.append(identity)
            # A round trip lets the server finish storing the preceding trace.
            ws.send("invalid")
            require(
                json.loads(ws.recv(timeout=timeout)).get("type") == "error",
                "Invalid JSON was not rejected",
            )
    report["checks"].append("WebSocket turns and reconnect")
    history = request(client, "GET", f"/v1/sessions/{session_id}").json()["messages"]
    require(
        [(m["role"], m["content"]) for m in history] == expected,
        "Cross-client stored history mismatch",
    )
    for index, identity in enumerate(identities):
        for message in history[index * 2 : index * 2 + 2]:
            require(
                message.get("metadata", {}).get("correlation") == identity,
                "Stored message correlation mismatch",
            )
        trace = request(client, "GET", f"/v1/traces/{identity['trace_id']}").json()
        require(
            trace.get("metadata", {}).get("correlation") == identity,
            "Stored trace correlation mismatch",
        )
    for key in ("request_id", "turn_id", "trace_id"):
        require(len({i[key] for i in identities}) == 3, f"Reused {key}")
    report["trace_ids"] = [i["trace_id"] for i in identities]
    report["checks"].append("Persisted history and three distinct matching traces")

    with connect_ws() as ws:
        ws.send(
            json.dumps(
                {
                    "message": "Do not execute",
                    "session_id": "missing-acceptance-session",
                }
            )
        )
        frame = json.loads(ws.recv(timeout=timeout))
        require(frame.get("type") == "error", "Missing session was accepted")
        require(
            not frame.get("correlation", {}).get("session_id"),
            "Missing session acquired verified identity",
        )
        request(client, "POST", "/v1/auth/logout")
        report["login_revoked"] = True
        ws.send(json.dumps({"message": "After logout", "session_id": session_id}))
        try:
            ws.recv(timeout=timeout)
        except ConnectionClosed as exc:
            require(
                exc.rcvd is not None and exc.rcvd.code == 1008,
                "Unexpected close code after logout",
            )
        else:
            raise CheckFailed("Revoked login still accepted WebSocket messages")
    report["checks"].append("Rejected missing session and revoked open socket")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--username")
    parser.add_argument(
        "--project-id", help="Existing project (prompt to select when omitted)"
    )
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
        or args.timeout <= 0
    ):
        parser.error(
            "Use an HTTP(S) server origin without credentials and a positive timeout"
        )
    base = args.url.rstrip("/")
    ws_url = (
        ("wss" if url.scheme == "https" else "ws")
        + "://"
        + url.netloc
        + "/v1/chat/stream"
    )
    report = {"status": "failed", "checks": [], "device_acceptance": "pending"}
    with httpx.Client(
        base_url=base, timeout=args.timeout, follow_redirects=False, trust_env=False
    ) as client:
        try:
            login = request(
                client,
                "POST",
                "/v1/auth/login",
                json={
                    "username": args.username or input("OpenJarvis username: "),
                    "password": getpass.getpass("OpenJarvis password: "),
                },
            ).json()
            token = login["session_token"]
            client.headers["X-OpenJarvis-Session"] = token

            project_id = args.project_id
            if not project_id:
                projects = request(client, "GET", "/v1/projects").json()["projects"]
                require(bool(projects), "Create a project in the web app first")
                for index, project in enumerate(projects, 1):
                    print(f"{index}. {project['name']}")
                choice = int(input("Project number for test conversation: "))
                require(1 <= choice <= len(projects), "Invalid project selection")
                project_id = projects[choice - 1]["project_id"]

            def connect_ws():
                return connect(
                    ws_url,
                    additional_headers={"X-OpenJarvis-Session": token},
                    open_timeout=10,
                    close_timeout=5,
                    proxy=None,
                )

            run_checks(
                client,
                connect_ws,
                project_id=project_id,
                model=args.model,
                timeout=args.timeout,
                report=report,
            )
            report["status"] = "passed"
        except CheckFailed as exc:
            report["error"] = str(exc)
        except Exception as exc:
            # Never include response bodies, credentials or raw transport errors.
            report["error"] = type(exc).__name__
        finally:
            if "X-OpenJarvis-Session" in client.headers and not report.get(
                "login_revoked"
            ):
                try:
                    response = client.post("/v1/auth/logout")
                    report["login_revoked"] = response.is_success
                except Exception:
                    report["login_revoked"] = False
                if not report["login_revoked"]:
                    report["status"] = "failed"
                    report["error"] = "Could not confirm test-login revocation"
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
