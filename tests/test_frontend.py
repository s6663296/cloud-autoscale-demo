import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "hub" / "static"


def test_static_has_no_external_resources():
    """前端不依賴外部資源：不得引用任何 http(s) 網址（SVG 命名空間除外）。"""
    for path in STATIC.rglob("*"):
        if path.suffix not in {".html", ".css", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        urls = [u for u in re.findall(r"https?://[^\s\"'`)]+", text) if not u.startswith("http://www.w3.org/")]
        assert not urls, f"{path.name}: {urls}"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 才能執行前端邏輯測試")
def test_frontend_logic():
    result = subprocess.run(
        ["node", "--test", *map(str, sorted((ROOT / "tests" / "js").glob("*.test.mjs")))],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
