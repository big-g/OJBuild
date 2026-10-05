"""Data-only previews. Never render active documents or execute file contents."""

from __future__ import annotations

import math
import re
import struct

TEXT_PREVIEW_BYTES = 64 * 1024
MAX_FACETS = 2000


def preview(filename: str, data: bytes) -> dict:
    if filename.lower().endswith(".stl"):
        geometry = _stl(data)
        if geometry is not None:
            return geometry
    sample = data[:TEXT_PREVIEW_BYTES]
    try:
        text = sample.decode("utf-8", errors="strict")
        if not any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            return dict(kind="text", text=text, truncated=len(data) > len(sample))
    except UnicodeDecodeError:
        # Do not mistake a UTF-8 sequence split at the preview boundary for binary.
        if len(data) > len(sample):
            try:
                text = sample[:-3].decode("utf-8")
                return dict(kind="text", text=text, truncated=True)
            except UnicodeDecodeError:
                pass
    return dict(kind="bytes", text=data[:512].hex(" "), truncated=len(data) > 512)


def _stl(data: bytes) -> dict | None:
    triangles = []
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if count and 84 + 50 * count == len(data):
            for index in range(min(count, MAX_FACETS)):
                values = struct.unpack_from("<9f", data, 84 + 50 * index + 12)
                triangles.append(list(values))
            if all(
                math.isfinite(v) and abs(v) <= 1e12 for tri in triangles for v in tri
            ):
                return dict(
                    kind="stl",
                    triangles=triangles,
                    facets=count,
                    truncated=count > MAX_FACETS,
                )
            return None
    # ASCII STL: only inspect a bounded prefix; regex does not run user code.
    text = data[: 512 * 1024].decode("ascii", errors="replace")
    if not text.lstrip().startswith("solid"):
        return None
    vertices = []
    number = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
    pattern = rf"\bvertex\s+({number})\s+({number})\s+({number})"
    for match in re.finditer(pattern, text):
        vertex = [float(v) for v in match.groups()]
        if not all(math.isfinite(v) and abs(v) <= 1e12 for v in vertex):
            return None
        vertices.extend(vertex)
        if len(vertices) == 9:
            triangles.append(vertices)
            vertices = []
            if len(triangles) == MAX_FACETS:
                break
    if not triangles:
        return None
    return dict(
        kind="stl",
        triangles=triangles,
        facets=None,
        truncated=len(data) > 512 * 1024 or len(triangles) == MAX_FACETS,
    )
