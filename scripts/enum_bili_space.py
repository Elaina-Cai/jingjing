# -*- coding: utf-8 -*-
"""慢性枚举 B 站 UP 主投稿列表（风控友好；断点续跑）。

- cookie: data/web_memes/bili_cookies.txt（由 yt-dlp 从 Firefox 导出）
- wbi 签名调 x/space/wbi/arc/search，每页 30 条；页间隔 --sleep 秒
- code != 0 或网络错：睡 --retry-sleep 秒后重试同一页
- 增量写 data/web_memes/video_list.tsv（bvid\ttitle\tcreated），可随时重启续跑
"""
import argparse
import hashlib
import http.cookiejar
import json
import pathlib
import random
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
COOKIE_FILE = ROOT / "data/web_memes/bili_cookies.txt"
MIXIN = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def opener_and_keys():
    jar = http.cookiejar.MozillaCookieJar(str(COOKIE_FILE))
    jar.load(ignore_discard=True, ignore_expires=True)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"User-Agent": UA, "Referer": "https://space.bilibili.com/94510621/video"},
    )
    nav = json.loads(opener.open(req, timeout=30).read().decode())
    img = nav["data"]["wbi_img"]["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = nav["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    return opener, "".join((img + sub)[i] for i in MIXIN)[:32]


def fetch_page(opener, mixin_key, mid, pn, ps=30):
    params = {
        "mid": str(mid), "ps": str(ps), "pn": str(pn),
        "order": "pubdate", "platform": "web", "web_location": "1550101",
        "wts": str(int(time.time())),
    }
    params = dict(sorted(params.items()))
    params = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in params.items()}
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    url = "https://api.bilibili.com/x/space/wbi/arc/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://space.bilibili.com/94510621/video"},
    )
    return json.loads(opener.open(req, timeout=30).read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", default="94510621")
    ap.add_argument("--out", default=str(ROOT / "data/web_memes/video_list.tsv"))
    ap.add_argument("--sleep", type=float, default=25.0)
    ap.add_argument("--retry-sleep", type=float, default=600.0)
    ap.add_argument("--max-pages", type=int, default=60)
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = {}
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if parts and parts[0].startswith("BV"):
                seen[parts[0]] = True
    log(f"resume: {len(seen)} videos already in {out}")
    opener, mixin_key = opener_and_keys()
    log(f"wbi ready, mixin={mixin_key[:12]}...")

    pn = 1
    while pn <= args.max_pages:
        try:
            payload = fetch_page(opener, mixin_key, args.mid, pn)
        except Exception as exc:
            log(f"page {pn} ERROR {type(exc).__name__}: {str(exc)[:110]} -> sleep {args.retry_sleep}s")
            time.sleep(args.retry_sleep)
            continue
        code = payload.get("code")
        if code != 0:
            log(f"page {pn} code={code} msg={payload.get('message')} -> sleep {args.retry_sleep}s")
            time.sleep(args.retry_sleep)
            continue
        data = payload.get("data") or {}
        vlist = ((data.get("list") or {}).get("vlist")) or []
        total = int((data.get("page") or {}).get("count") or 0)
        added = 0
        with out.open("a", encoding="utf-8") as fh:
            for v in vlist:
                bv = str(v.get("bvid") or "")
                if not bv or bv in seen:
                    continue
                fh.write(f"{bv}\t{str(v.get('title') or '')}\t{int(v.get('created') or 0)}\n")
                seen[bv] = True
                added += 1
        log(f"page {pn}: +{added} | total {len(seen)}/{total}")
        if not vlist or (total and len(seen) >= total):
            break
        pn += 1
        time.sleep(args.sleep + random.random() * 10)
    log(f"DONE {len(seen)} videos -> {out}")


if __name__ == "__main__":
    main()
