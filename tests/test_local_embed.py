"""Pin the dependency-free local embedder against sentence-transformers goldens.

standalone style: ``python tests/test_local_embed.py``.
The golden vectors in tests/golden/local_embed_bge_zh.json were produced with
sentence-transformers (normalize_embeddings=True) for the cached
BAAI/bge-large-zh-v1.5 snapshot; the local torch/tokenizers implementation must
reproduce them (cosine >= 0.999). Skipped when the model is not cached locally.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.memory import local_embed  # noqa: E402

GOLDEN = ROOT / "tests" / "golden" / "local_embed_bge_zh.json"
COS_MIN = 0.999


def _load():
    with open(GOLDEN, encoding="utf-8") as fh:
        return json.load(fh)


def _cos(a, b) -> float:
    import numpy as np

    va = np.asarray(a, dtype="float64")
    vb = np.asarray(b, dtype="float64")
    va = va / (np.linalg.norm(va) or 1.0)
    vb = vb / (np.linalg.norm(vb) or 1.0)
    return float(va @ vb)


def test_golden_vectors_match_sentence_transformers() -> bool:
    if not local_embed.available():
        print("    skip: local embed model not cached")
        return True
    data = _load()
    cases = data["cases"]
    vectors = local_embed.embed_texts([c["text"] for c in cases])
    if vectors.shape != (len(cases), 1024):
        print("    fail: unexpected shape %s" % (vectors.shape,))
        return False
    worst = 1.0
    for index, case in enumerate(cases):
        cos = _cos(vectors[index], case["vector"])
        worst = min(worst, cos)
        if cos < COS_MIN:
            print("    fail: case %d cos=%.6f < %.3f" % (index, cos, COS_MIN))
            return False
    print("    golden cases=%d worst_cos=%.6f" % (len(cases), worst))
    return True


def test_unit_norm_and_dim() -> bool:
    if not local_embed.available():
        return True
    import numpy as np

    vectors = local_embed.embed_texts(["test one", "test two"])
    norms = np.linalg.norm(vectors, axis=1)
    ok = vectors.shape[1] == 1024 and float(np.abs(norms - 1.0).max()) < 1e-5
    print("    dim=%d norm_err=%.2e" % (vectors.shape[1], float(np.abs(norms - 1.0).max())))
    return bool(ok)


def test_determinism() -> bool:
    if not local_embed.available():
        return True
    import numpy as np

    first = local_embed.embed_texts(["deterministic text"])
    second = local_embed.embed_texts(["deterministic text"])
    diff = float(np.abs(first - second).max())
    print("    max_abs_diff=%.2e" % diff)
    return diff < 1e-6


def test_degrades_without_model_directory() -> bool:
    if not local_embed.available():
        return True
    ok = local_embed.available("Z:/definitely/missing/dir") is False
    ok = ok and local_embed.available(str(ROOT / "docs")) is False
    print("    available(bad)=%s" % ok)
    return bool(ok)


def test_empty_and_ascii_inputs_do_not_raise() -> bool:
    if not local_embed.available():
        return True
    vectors = local_embed.embed_texts(["", "hello world"])
    print("    shape=%s" % (vectors.shape,))
    return vectors.shape == (2, 1024)


def main() -> bool:
    names = sorted(name for name in list(globals()) if name.startswith("test_"))
    ok = True
    for name in names:
        try:
            result = bool(globals()[name]())
        except Exception as exc:  # noqa: BLE001
            print("  FAIL %s: %s: %s" % (name, type(exc).__name__, exc))
            result = False
        print(("ok   " if result else "FAIL ") + name)
        ok = ok and result
    print("local embed: %d tests, %s" % (len(names), "all passed" if ok else "FAILED"))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
