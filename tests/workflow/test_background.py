"""Foreground masks are explicit, bounded, and never silently bypassed."""

import io

import pytest
from PIL import Image

from openjarvis.workflow.background_worker import process
from openjarvis.workflow.providers import remove_background


def png(alpha=False):
    image = Image.new("RGBA", (32, 32), (0, 0, 0, 0) if alpha else "white")
    image.paste((255, 0, 0, 255), (8, 8, 24, 24))
    result = io.BytesIO()
    image.save(result, "PNG")
    return result.getvalue()


def test_existing_alpha_preserved_in_real_subprocess(monkeypatch):
    monkeypatch.setenv("OPENJARVIS_BACKGROUND_MODEL", "/missing/model.onnx")
    source = png(True)
    result, report = remove_background(source, {"preserve_alpha": True})
    assert report["preserved_alpha"] is True
    assert report["foreground_fraction"] == 0.25
    assert (
        Image.open(io.BytesIO(result)).tobytes()
        == Image.open(io.BytesIO(source)).tobytes()
    )


def test_missing_model_fails_without_download(monkeypatch):
    monkeypatch.setenv("OPENJARVIS_BACKGROUND_MODEL", "/missing/model.onnx")
    with pytest.raises(ValueError, match="model is missing"):
        remove_background(png(), {})
    with pytest.raises(ValueError, match="model is missing"):
        process(png(True), {"preserve_alpha": False})


def test_wrong_model_rejected_before_inference(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"wrong model")
    monkeypatch.setenv("OPENJARVIS_BACKGROUND_MODEL", str(model))
    with pytest.raises(ValueError, match="checksum mismatch"):
        process(png(), {})


def test_empty_alpha_rejected():
    data = io.BytesIO()
    Image.new("RGBA", (8, 8)).save(data, "PNG")
    with pytest.raises(ValueError, match="entirely transparent"):
        process(data.getvalue(), {})
