"""Test data for the vector basemap — real files, not mocks."""

from __future__ import annotations

from pathlib import Path


def makeStyleDir(path: Path) -> Path:
    """Style directory with the two files vectorbasemap looks for."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "otm_layers.json").write_text("const otm_layers = [];\n")
    (path / "otm_style.js").write_text("function otmVectorStyle() { return {}; }\n")
    return path
