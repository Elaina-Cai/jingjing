# 仓库卫生：私有资产不许被 git 跟踪（2026-10-26）
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN = (".pth", ".pt", ".ckpt", ".safetensors", ".index", ".onnx", ".wav", ".mp3", ".flac")
MAX_BYTES = 2 * 1024 * 1024
ALLOW_BIG = {"data/web_memes/memes_index.json"}
MUST_BE_IGNORED = (
    "data/models/rvc/x.pth",
    "data/models/cosyvoice3/llm.pt",
    "data/persona/assets/private/demo/faces/pack.json",
    "data/persona/assets/jingjing/voice/pack.json",
)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def _tracked() -> list:
    out = _git("ls-files", "-z")
    return [x for x in out.split("\0") if x]


def test_no_model_or_audio_files_tracked():
    bad = [f for f in _tracked() if f.lower().endswith(FORBIDDEN)]
    assert bad == [], "这些私有资产被 git 跟踪了：%r" % bad[:10]


def test_no_tracked_file_over_limit():
    big = []
    for f in _tracked():
        if f in ALLOW_BIG:
            continue
        p = REPO / f
        try:
            if p.is_file() and p.stat().st_size > MAX_BYTES:
                big.append((f, p.stat().st_size))
        except OSError:
            continue
    assert big == [], "有超过 2MB 的被跟踪文件：%r" % big[:5]


def test_private_zones_are_ignored():
    if not (REPO / ".git").exists():
        return
    for rel in MUST_BE_IGNORED:
        out = _git("check-ignore", "--no-index", rel)
        assert out.strip(), "%s 没有被 .gitignore 忽略" % rel


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
