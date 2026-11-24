"""飞鱼音色引擎的客户端逻辑（不碰 WSL、不碰网络）。"""
import pathlib

import pytest

from core.speech import tts_cosyvoice as cv3


def _fake_pack(tmp_path, *, complete=True):
    pack = tmp_path / "feiyu"
    base = pack / "models" / "Fun-CosyVoice3-0.5B-2512"
    base.mkdir(parents=True)
    files = {
        base / "llm.pt": 4096,
        base / "flow.pt": 4096,
        base / "hift.pt": 4096,
        base / "CosyVoice-BlankEN" / "model.safetensors": 4096,
        base / "speech_tokenizer_v3.onnx": 4096,
        base / "campplus.onnx": 4096,
        pack / "models" / "feiyu_cute_F6_0040.pt": 4096,
        pack / "references" / "cute_N01.wav": 4096,
        pack / "references" / "gentle_N10.wav": 4096,
    }
    for path, size in files.items():
        if not complete and path.name == "llm.pt":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"0" * size)
    return pack


def test_missing_files_reports_llm(tmp_path, monkeypatch):
    pack = _fake_pack(tmp_path, complete=False)
    monkeypatch.setattr(cv3, "pack_dir", lambda: pack)
    assert cv3.missing_files() == ["llm"]
    monkeypatch.setattr(cv3.shutil, "which", lambda name: None)
    assert cv3.available() is False


def test_available_when_pack_and_wsl_present(tmp_path, monkeypatch):
    pack = _fake_pack(tmp_path)
    monkeypatch.setattr(cv3, "pack_dir", lambda: pack)
    monkeypatch.setattr(cv3.shutil, "which", lambda name: "C:/Windows/System32/wsl.exe")
    assert cv3.missing_files() == []
    assert cv3.available() is True
    assert cv3.status()["available"] is True


def test_wsl_path_is_mnt_and_single_slash():
    assert cv3.wsl_path("C:\\Users\\Elaina\\a b.wav") == "/mnt/c/Users/Elaina/a b.wav"
    assert cv3.wsl_path("C:\\\\Users\\\\Elaina\\\\x.wav") == "/mnt/c/Users/Elaina/x.wav"
    assert cv3.wsl_path("/home/elaina/x.wav") == "/home/elaina/x.wav"


def test_request_returns_none_when_connection_refused(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr(cv3.socket, "create_connection", boom)
    assert cv3._request({"op": "ping"}, timeout=0.1) is None
    assert cv3.ping(timeout=0.1) is False
    assert cv3.stop_server() is False


def test_ping_accepts_ready_reply(monkeypatch):
    monkeypatch.setattr(cv3, "_request", lambda payload, timeout=10.0: {"ok": True, "ready": True})
    assert cv3.ping() is True


def test_ping_rejects_unready_reply(monkeypatch):
    monkeypatch.setattr(cv3, "_request", lambda payload, timeout=10.0: {"ok": True, "ready": False})
    assert cv3.ping() is False


def test_synth_returns_none_when_unavailable(tmp_path, monkeypatch):
    pack = _fake_pack(tmp_path, complete=False)
    monkeypatch.setattr(cv3, "pack_dir", lambda: pack)
    monkeypatch.setattr(cv3.shutil, "which", lambda name: None)
    assert cv3.synth("你好呀") is None


def test_synth_hits_cache_without_touching_server(tmp_path, monkeypatch):
    pack = _fake_pack(tmp_path)
    monkeypatch.setattr(cv3, "pack_dir", lambda: pack)
    monkeypatch.setattr(cv3.shutil, "which", lambda name: "wsl.exe")
    monkeypatch.setattr(cv3, "CACHE_DIR", tmp_path / "cache")
    key = cv3.cache_key("你好呀", style="cute", speed=1.0, seed=42)
    cached = tmp_path / "cache" / ("cv3_%s.wav" % key)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"0" * 4096)

    def explode(*args, **kwargs):
        raise AssertionError("命中缓存时不该连服务")

    monkeypatch.setattr(cv3, "_request", explode)
    assert cv3.synth("你好呀") == cached


def test_synth_writes_through_server(tmp_path, monkeypatch):
    pack = _fake_pack(tmp_path)
    monkeypatch.setattr(cv3, "pack_dir", lambda: pack)
    monkeypatch.setattr(cv3.shutil, "which", lambda name: "wsl.exe")
    target = tmp_path / "out.wav"

    def fake_request(payload, *, timeout=10.0):
        if payload.get("op") == "ping":
            return {"ok": True, "ready": True}
        if payload.get("op") == "say":
            assert payload["style"] == "cute"
            assert payload["out"].startswith("/mnt/c/")  # 传给服务的是 WSL 侧路径
            target.write_bytes(b"1" * 4096)            # 服务写完，文件落回 Windows 侧
            return {"ok": True, "seconds": 1.5, "runtime_s": 0.9}
        return None

    monkeypatch.setattr(cv3, "_request", fake_request)
    got = cv3.synth("你好呀", out_path=str(target))
    assert got == target
    assert target.stat().st_size == 4096


def test_synth_returns_none_when_server_says_error(tmp_path, monkeypatch):
    pack = _fake_pack(tmp_path)
    monkeypatch.setattr(cv3, "pack_dir", lambda: pack)
    monkeypatch.setattr(cv3.shutil, "which", lambda name: "wsl.exe")
    monkeypatch.setattr(cv3, "_request",
                        lambda payload, timeout=10.0: {"ok": False, "error": "no audio"})
    # 服务没起时绝不能真的去 spawn wsl.exe（那会轮询 BOOT_WAIT_S 秒），这里直接当成起不来
    monkeypatch.setattr(cv3, "start_server", lambda *a, **k: False)
    assert cv3.synth("你好呀", out_path=str(tmp_path / "x.wav")) is None


def test_cache_key_changes_with_style_and_seed(tmp_path, monkeypatch):
    pack = _fake_pack(tmp_path)
    monkeypatch.setattr(cv3, "pack_dir", lambda: pack)
    a = cv3.cache_key("你好呀", style="cute", seed=42)
    b = cv3.cache_key("你好呀", style="gentle", seed=42)
    c = cv3.cache_key("你好呀", style="cute", seed=7)
    assert len({a, b, c}) == 3


def test_engine_registers_cosyvoice3_first():
    import core.speech.tts_engine as eng

    assert eng.ENGINES[0] == "cosyvoice3"
    assert set(eng.ENGINES) == {"cosyvoice3", "edge", "sapi"}


def test_engine_falls_back_when_cosyvoice3_missing(tmp_path, monkeypatch):
    import core.speech.tts_engine as eng

    fake = tmp_path / "fake.wav"
    fake.write_bytes(b"0" * 2048)
    # 只留 sapi：cosyvoice3 不在、edge 也当成不在，避免真的去连微软（会 hang）
    monkeypatch.setattr(eng, "available", lambda name="": str(name or "").lower() == "sapi")
    monkeypatch.setattr(eng, "_synth_edge", lambda *a, **k: pytest.fail("不该走 edge"))
    monkeypatch.setattr(eng, "_synth_sapi", lambda *a, **k: fake)
    got, used = eng.synth_ex("你好呀", engine="cosyvoice3")
    assert got == fake
    assert used == "sapi"
    assert eng.last_engine() == "sapi"


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
