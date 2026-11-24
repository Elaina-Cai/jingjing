# -*- coding: utf-8 -*-
"""回归入口：3D 贴图文字必须与设计文档口径一致（见 scripts/check_3d_textures.py）。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "check_3d_textures.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.rstrip())
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
