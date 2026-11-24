"""旧投影库整体弃置后，召回必须短路（2026-11-16 主人裁决）。"""
import os

import pytest

from core.terminal import memory_gateway as gw


def test_env_flag_is_read(monkeypatch):
    monkeypatch.setenv("YINYUE_LEGACY_PROJECTIONS_DISCARDED", "1")
    assert gw.legacy_projections_discarded() is True
    monkeypatch.setenv("YINYUE_LEGACY_PROJECTIONS_DISCARDED", "on")
    assert gw.legacy_projections_discarded() is True
    monkeypatch.setenv("YINYUE_LEGACY_PROJECTIONS_DISCARDED", "0")
    assert gw.legacy_projections_discarded() is False
    monkeypatch.delenv("YINYUE_LEGACY_PROJECTIONS_DISCARDED", raising=False)
    assert gw.legacy_projections_discarded() is False


def test_discarded_gateway_never_touches_projections():
    class Boom:
        def query_candidates(self, *args, **kwargs):
            raise AssertionError("discarded legacy projections must not be scanned")

        def get(self, *args, **kwargs):
            raise AssertionError("discarded legacy projections must not be read")

    gateway = gw.MemoryGateway(projection_repository=Boom(), legacy_discarded=True)
    assert gateway.legacy_discarded is True
    assert gateway.recall(None, query="任何旧事") == []


def test_default_gateway_keeps_reading(monkeypatch):
    monkeypatch.delenv("YINYUE_LEGACY_PROJECTIONS_DISCARDED", raising=False)
    gateway = gw.MemoryGateway(projection_repository=object())
    assert gateway.legacy_discarded is False
