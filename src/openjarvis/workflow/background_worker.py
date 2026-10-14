"""CPU-only foreground extraction; models are installed by the administrator."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

MODEL_MD5 = "60024c5c889badc19c04ad937298a77b"
DEFAULT_MODEL = "/mnt/ai/openjarvis/models/background/u2net.onnx"


def process(data, params):
    from PIL import Image, ImageOps

    if len(data) > 20 * 1024**2:
        raise ValueError("Image exceeds 20 MiB")
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > 16_000_000:
            raise ValueError("Image exceeds 16 megapixels")
        image = ImageOps.exif_transpose(source).convert("RGBA")
    alpha = image.getchannel("A")
    low, high = alpha.getextrema()
    if high == 0:
        raise ValueError("Image is entirely transparent")
    preserved = params.get("preserve_alpha", True) and low < 128 and high > 128
    if not preserved:
        model = Path(os.environ.get("OPENJARVIS_BACKGROUND_MODEL", DEFAULT_MODEL))
        if not model.is_file():
            raise ValueError(
                "Background model is missing; run install-background-removal.sh"
            )
        with model.open("rb") as handle:
            digest = hashlib.file_digest(handle, "md5").hexdigest()
        if digest != MODEL_MD5:
            raise ValueError("Background model checksum mismatch; reinstall the model")
        from rembg import new_session, remove

        session = new_session(
            "u2net_custom", model_path=str(model), providers=["CPUExecutionProvider"]
        )
        image = remove(image, session=session).convert("RGBA")
        alpha = image.getchannel("A")
    histogram = alpha.histogram()
    pixels = image.width * image.height
    foreground = sum(histogram[128:])
    if not foreground or foreground == pixels:
        raise ValueError(
            "No usable foreground mask; use a clearer image "
            "or a prepared transparent PNG"
        )
    output = io.BytesIO()
    image.save(output, format="PNG")
    if output.tell() > 20 * 1024**2:
        raise ValueError("Foreground PNG exceeds 20 MiB")
    return output.getvalue(), {
        "operation": "image_remove_background",
        "preserved_alpha": bool(preserved),
        "model": "existing alpha" if preserved else "u2net (CPU)",
        "width": image.width,
        "height": image.height,
        "foreground_fraction": round(foreground / pixels, 4),
        "review": (
            "Inspect the transparent preview before 3D generation; "
            "segmentation can remove object details."
        ),
    }


if __name__ == "__main__":
    import contextlib
    import resource
    import sys

    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (240, 240))
    resource.setrlimit(resource.RLIMIT_FSIZE, (20 * 1024**2, 20 * 1024**2))
    try:
        with contextlib.redirect_stdout(sys.stderr):
            data, report = process(
                Path(sys.argv[1]).read_bytes(), json.loads(sys.argv[3])
            )
            Path(sys.argv[2]).write_bytes(data)
        print(json.dumps(report, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:300]}))
        raise SystemExit(1) from None
