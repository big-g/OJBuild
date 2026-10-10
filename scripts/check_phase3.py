#!/usr/bin/env python3
"""Run saved GLB→STL workflow acceptance against the existing API, retaining results."""

from __future__ import annotations

import argparse
import base64
import getpass
import io
import time
from urllib.parse import urlsplit

import httpx
import numpy as np
import trimesh


def request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if not response.is_success:
        raise RuntimeError(
            f"{method} {path}: HTTP {response.status_code}; review API/server logs"
        )
    return response


def checks(client, timeout):
    catalog = request(client, "GET", "/v1/workflows").json()
    definition = catalog["templates"][0]
    enabled = {op["id"] for op in catalog["operations"] if op["enabled"]}
    required = {step["operation"] for step in definition["steps"]}
    if not required <= enabled:
        raise RuntimeError(
            "An administrator must approve inspect, repair, scale and STL export "
            "in the workflow page"
        )
    original = trimesh.creation.box(extents=[1, 2, 3]).export(file_type="glb")
    artifact = request(
        client,
        "POST",
        "/v1/files",
        json={
            "filename": "phase3-original.glb",
            "encoding": "base64",
            "content": base64.b64encode(original).decode("ascii"),
        },
    ).json()
    definition["name"] = "Phase 3 acceptance " + time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime()
    )
    saved = request(
        client, "POST", "/v1/workflows", json={"definition": definition}
    ).json()
    run_ids = []
    for target in (100.0, 60.0):
        if target == 60.0:
            definition["steps"][2]["parameters"]["target_mm"] = target
            saved = request(
                client,
                "POST",
                "/v1/workflows",
                json={
                    "id": saved["id"],
                    "revision": saved["revision"],
                    "definition": definition,
                },
            ).json()
        run = request(
            client,
            "POST",
            "/v1/workflows/runs",
            json={
                "workflow_id": saved["id"],
                "revision": saved["revision"],
                "input": {"artifact_id": artifact["id"]},
            },
        ).json()
        run_ids.append(run["id"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = request(client, "GET", "/v1/workflows/runs/" + run["id"]).json()
            if run["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(1)
        if run["status"] != "completed":
            raise RuntimeError(
                f"Run {run['id']} ended as {run['status']}; "
                "inspect its step errors in the workflow page"
            )
        output = run["output"]
        if not output["report"].get("validated_export") or output.get("units") != "mm":
            raise RuntimeError("Export validation or explicit units missing")
        stl = request(
            client, "GET", "/v1/files/" + output["artifact_id"] + "/download"
        ).content
        mesh = trimesh.load(io.BytesIO(stl), file_type="stl")
        if not mesh.is_watertight or not np.allclose(
            mesh.extents, [target / 3, target * 2 / 3, target], rtol=1e-5
        ):
            raise RuntimeError(
                "Downloaded STL failed independent geometry/dimension validation"
            )
        for step in run["steps"]:
            report_id = step["output"]["report_artifact"]["id"]
            report = request(
                client, "GET", "/v1/files/" + report_id + "/download"
            ).json()
            if report["operation"] != step["operation"] or "before" not in report:
                raise RuntimeError("Missing per-step inspection report")
        print(
            f"PASS: revision {saved['revision']}, longest dimension {target:g} mm, "
            f"run {run['id']}",
            flush=True,
        )
    retained = request(
        client, "GET", "/v1/files/" + artifact["id"] + "/download"
    ).content
    if retained != original:
        raise RuntimeError("Original GLB changed")
    print(
        "PASS: original preserved; both revisions, reports and files "
        "retained for review.",
        flush=True,
    )
    return run_ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--username")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or args.timeout <= 0
    ):
        parser.error("Use an HTTP(S) origin without credentials and a positive timeout")
    print("Python started. Checking Phase 3 on " + args.url, flush=True)
    with httpx.Client(
        base_url=args.url.rstrip("/"),
        timeout=30,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        token = None
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
            checks(client, args.timeout)
        except Exception as exc:
            print("FAIL: " + str(exc), flush=True)
            return 1
        finally:
            if token:
                request(client, "POST", "/v1/auth/logout")
    print(
        "Mesh acceptance passed. GPU/model workers, optional Blender and "
        "slicer review still require server acceptance.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
