"""Test data for the vector basemap — real files, not mocks."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def makeMbtiles(path: Path, rows=None, metadata=None) -> Path:
    """Write a minimal but real mbtiles file so existence checks see a real SQLite DB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        conn.execute(
            "CREATE TABLE tiles ("
            "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"
        )
        conn.executemany(
            "INSERT INTO metadata (name, value) VALUES (?, ?)",
            sorted((metadata or {}).items()),
        )
        if rows:
            conn.executemany(
                "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
                rows,
            )
    conn.close()
    return path


def makeStyleDir(path: Path) -> Path:
    """Style directory with the two files vectorbasemap looks for."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "otm_layers.json").write_text("const otm_layers = [];\n")
    (path / "otm_style.js").write_text("function otmVectorStyle() { return {}; }\n")
    return path
