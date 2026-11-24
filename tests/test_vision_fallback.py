"""Vision provider selection checks.

Run directly: python tests/test_vision_fallback.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.client import LLMClient, _PROVIDER_REGISTRY, _model_supports_vision


def main() -> bool:
    ok = True

    ok &= _model_supports_vision("qwen2.5-vl:7b") is True
    ok &= _model_supports_vision("llava:13b") is True
    ok &= _model_supports_vision("minicpm-v") is True
    ok &= _model_supports_vision("qwen2.5:7b") is False
    ok &= _model_supports_vision("") is False

    LLMClient()  # populate the provider registry
    ok &= bool(_PROVIDER_REGISTRY.get("deepseek", {}).get("supports_vision"))
    ok &= bool(_PROVIDER_REGISTRY.get("dashscope", {}).get("supports_vision"))

    print("vision fallback checks:", "ok" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
