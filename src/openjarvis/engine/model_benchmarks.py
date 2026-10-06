"""Small, fixed diagnostic probes; no user prompts, code or tools are executed."""

import base64
import hashlib
import json
import struct
import time
import zlib

import httpx

from openjarvis.engine._base import EngineConnectionError
from openjarvis.engine.configured_models import ConfiguredModelEngine
from openjarvis.engine.connection_discovery import read_json
from openjarvis.engine.task_routing import SUITE_VERSION, TASKS

PROBES = {
    "general": (
        'Return only JSON. Set "answer" to the list of these words in alphabetical '
        "order, lowercase: PEAR, APPLE, BANANA. No other keys.",
        ["apple", "banana", "pear"],
    ),
    "coding": (
        'Return only JSON with one key "answer". Its value must be a list of three '
        "results in order: sum(range(5)); [x*x for x in range(4) if x % 2 == 0]; "
        "len(set([1,1,2,3,3])). Evaluate mentally; do not run code.",
        [10, [0, 4], 3],
    ),
    "analysis": (
        'Return only JSON with one key "answer" whose value is a list: '
        "First, if all A are B and no B are C, can any A be C? Use a boolean. "
        "Second, three jobs take 2, 3 and 5 minutes sequentially; how many minutes "
        "total? Third, how many of integers 1 through 10 inclusive are divisible "
        "by 3? No other keys.",
        [False, 10, 3],
    ),
    "vision": (
        "What single dominant color fills the attached image? Return only JSON "
        'with one key "answer", set to a lowercase English color word.',
        "red",
    ),
}
TOOL = {
    "type": "function",
    "function": {
        "name": "diagnostic_echo",
        "description": "Return the exact provided marker.",
        "parameters": {
            "type": "object",
            "properties": {"marker": {"type": "string", "enum": ["oj_probe_v1"]}},
            "required": ["marker"],
            "additionalProperties": False,
        },
    },
}


def probe_image():
    """Deterministic 32x32 red PNG test fixture, constructed without external input."""

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    raw = b"\x89PNG\r\n\x1a\n"
    raw += chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 32) * 32))
    raw += chunk(b"IEND", b"")
    return base64.b64encode(raw).decode("ascii")


def _answer_ok(result, expected):
    text = result.get("content", "")
    if not isinstance(text, str) or len(text) > 8192:
        return False
    try:
        # Strict JSON and type comparison, so True cannot pass as integer 1.
        actual = json.loads(text)
        return (
            isinstance(actual, dict)
            and set(actual) == {"answer"}
            and json.dumps(actual["answer"]) == json.dumps(expected)
            and result.get("finish_reason", "stop") == "stop"
        )
    except (ValueError, RecursionError):
        return False


def _tool_ok(result):
    calls = result.get("tool_calls", [])
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return False
    function = calls[0].get("function", {})
    if not isinstance(function, dict):
        return False
    args = function.get("arguments")
    try:
        args = json.loads(args) if isinstance(args, str) else args
    except (ValueError, RecursionError):
        return False
    return (
        function.get("name") == "diagnostic_echo"
        and args == {"marker": "oj_probe_v1"}
        and result.get("finish_reason", "stop") != "length"
    )


def run_diagnostic(connections, selected, task, revision):
    if task not in TASKS:
        raise ValueError("Unknown diagnostic task")
    engine = ConfiguredModelEngine(connections, selected)
    if engine.connection["revision"] != revision:
        raise ValueError("Connection changed. Reload before running diagnostics")
    engine.check(images=task == "vision")
    caps = next(
        m for m in engine.connection["catalog"] if m["serving_id"] == engine.serving_id
    )["capabilities"]
    prompt, expected = PROBES[task]
    started = time.monotonic()
    details = {
        "task_passed": False,
        "tools_passed": False,
        "tools_tested": "tools" in caps,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "suite_version": SUITE_VERSION,
        "failure": "",
    }
    tokens = 0

    def call(content, *, images=None, tools=None, max_tokens=512):
        nonlocal tokens
        engine.check(images=bool(images), tools=bool(tools), live=True)
        body = {
            "model": engine.serving_id,
            "stream": False,
            "think": False,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                    **({"images": images} if images else {}),
                }
            ],
            "options": {"temperature": 0, "num_predict": max_tokens},
            **({"tools": tools} if tools else {}),
        }
        data = read_json(engine.connection, "/api/chat", "POST", body)
        engine._row()
        if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
            raise ValueError("Invalid diagnostic response")
        counts = [data.get("prompt_eval_count", 0), data.get("eval_count", 0)]
        if not all(type(count) is int and 0 <= count <= 2**31 for count in counts):
            raise ValueError("Invalid diagnostic usage")
        tokens += sum(counts)
        return {**data["message"], "finish_reason": data.get("done_reason", "stop")}

    try:
        result = call(prompt, images=[probe_image()] if task == "vision" else [])
        details["task_passed"] = _answer_ok(result, expected)
        if "tools" in caps and time.monotonic() - started < 45:
            result = call(
                "Call diagnostic_echo exactly once with marker oj_probe_v1. "
                "Do not answer in text.",
                max_tokens=128,
                tools=[TOOL],
            )
            details["tools_passed"] = _tool_ok(result)
        if not details["task_passed"]:
            details["failure"] = "Task diagnostic did not match the expected JSON"
        elif details["tools_tested"] and not details["tools_passed"]:
            details["failure"] = (
                "Reported tool support did not pass the tool-call probe"
            )
    except (
        EngineConnectionError,
        httpx.HTTPError,
        OSError,
        ValueError,
        RecursionError,
    ):
        details["failure"] = "Selected model unavailable or rejected the diagnostic"
    return (
        engine.connection,
        engine.serving_id,
        {
            "passed": details["task_passed"]
            and (not details["tools_tested"] or details["tools_passed"]),
            "details": details,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "tokens": tokens,
        },
    )
