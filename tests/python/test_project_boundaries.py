from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_required_v1_boundaries_exist() -> None:
    required = [
        "apps/web",
        "apps/api",
        "src/focusmate/core",
        "src/focusmate/adapters",
        "services/oj_worker",
        "skills",
        "schemas",
        "tests/node",
        "tests/python",
    ]
    assert all((ROOT / path).is_dir() for path in required)


def test_legacy_mvp_remains_isolated() -> None:
    source_suffixes = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py"}
    active_files = [
        path
        for base in (ROOT / "apps", ROOT / "src", ROOT / "services", ROOT / "tools")
        for path in base.rglob("*")
        if path.is_file() and path.suffix in source_suffixes
    ]
    for path in active_files:
        assert "test/legacy-mvp" not in path.read_text(encoding="utf-8")
