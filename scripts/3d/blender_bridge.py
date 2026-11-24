"""Blender 直连桥（TCP 9876，扁平协议）最小客户端。"""
import json, socket, struct, sys, time

HOST, PORT = "127.0.0.1", 9876


def _recv_json(sock, deadline):
    buf = b""
    while True:
        if time.time() > deadline:
            raise TimeoutError("no complete json within deadline; got %d bytes" % len(buf))
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("closed by peer")
        buf += chunk
        try:
            return json.loads(buf.decode("utf-8", "replace"))
        except Exception:
            continue


def call(msg, timeout=120.0):
    with socket.create_connection((HOST, PORT), timeout=10) as s:
        s.settimeout(timeout)
        s.sendall(json.dumps(msg).encode("utf-8") + b"\n")
        return _recv_json(s, time.time() + timeout)


def ping():
    return call({"type": "ping", "params": {}}, 20)


def scene_info():
    return call({"type": "get_scene_info", "params": {}}, 30)


def execute(code, timeout=180.0):
    r = call({"type": "execute_code", "params": {"code": code}}, timeout)
    if isinstance(r, dict) and r.get("status") == "error":
        raise RuntimeError(r.get("message") or r)
    return r


def shot(path, max_size=1400):
    r = call({"type": "get_viewport_screenshot",
              "params": {"max_size": max_size, "filepath": str(path), "format": "png"}}, 60)
    return r


if __name__ == "__main__":
    print("ping ->", json.dumps(ping(), ensure_ascii=False)[:200])
