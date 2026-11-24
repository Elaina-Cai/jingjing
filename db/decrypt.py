import hmac
import json
import structlog
import sqlite3
import shutil
import hashlib
import os
from pathlib import Path
from typing import Optional

from Cryptodome.Cipher import AES
from safelog import warn
import time
from core.infra.envflags import text

log = structlog.get_logger()

SQLITE_HEADER = b"SQLite format 3\x00"
PAGE_SZ = 4096
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = (IV_SZ + HMAC_SZ + 15) // 16 * 16

_SIGNATURE_FILE = ".decrypt_signatures.json"


# 2026-10-28：即便指纹没变，也最多 _REFRESH_S 秒重解一次（防 mtime 不动的极端情况）。
_REFRESH_S = float(text("YINYUE_DECRYPT_REFRESH_S", "3") or 3)


def _db_signature(path: Path) -> list:
    # 2026-10-27：从 [mtime_ns, size] 改成「size + 10 分钟一格的 mtime 桶」。
    # 原因：微信每写一次库就改 mtime（一天 400+ 次），旧判据等于每轮都判"变了"，
    # 增量解密形同虚设（实测 decrypt_merge_done 1575 次/天 = 几乎每轮）。
    # 消息库是追加写，来新消息体积必涨；同尺寸改写（VACUUM / WAL checkpoint）
    # 靠 mtime 桶兜底，最多延迟 10 分钟。
    stat = path.stat()
    # 2026-10-28 根治：桶会让"同一格内"的写入看不见（P6那句卡了 11 分钟）。
    # 微信复用 SQLite 空闲页 → 写新消息 size 不变，只有 mtime 变；
    # 所以 mtime 必须用精确值，另配 _REFRESH_S 的时间兜底（见下）。
    return [int(stat.st_size), int(stat.st_mtime_ns)]


def _load_signatures(out: Path) -> dict:
    try:
        return json.loads((out / _SIGNATURE_FILE).read_text(encoding="utf-8"))
    except Exception:
        warn("db/decrypt.py:33 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}


def _save_signatures(out: Path, signatures: dict) -> None:
    try:
        (out / _SIGNATURE_FILE).write_text(
            json.dumps(signatures, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.debug("decrypt_signature_save_failed", error=str(exc))


def _plausible_key(raw: bytes) -> bool:
    """是不是一把真的 SQLCipher 密钥（还是内存里撞到的假阳性）。

    2026-10-21 真机事故：独立进程里 `get_wx_key()` 返回了 `00010203...`，
    也就是把内存里一段 ASCII 数字串当成候选，解出来正好 32 字节（0x00,0x01,0x02…），
    于是「密钥拿到了」——用它去解密只会得到 no_db_found / hmac 对不上，
    而日志里看起来一切正常。真密钥是随机的，不会只有极少种取值。
    """
    if len(raw) != 32:
        return False
    if raw == bytes(range(32)) or raw == b"\x00" * 32:
        return False
    if len(set(raw)) < 16:
        return False
    # 严格递增（0,1,2,…,31）或严格递减都不是随机密钥
    if all(b - a == 1 for a, b in zip(raw, raw[1:])):
        return False
    return True


def get_wx_key() -> Optional[str]:
    try:
        import pymem
        import subprocess

        pid = None
        for proc_name in ["WeChatAppEx.exe", "WeChat.exe"]:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in result.stdout.strip().split("\n"):
                if proc_name in line:
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2:
                        try:
                            pid = int(parts[1])
                            break
                        except ValueError:
                            warn("db/decrypt.py:84 ValueError", err=repr(__import__("sys").exc_info()[1]))
                            continue
            if pid:
                break

        if not pid:
            log.error("wechat_not_running")
            return None

        pm = pymem.Pymem()
        pm.open_process_from_id(pid)
        base_addr = pm.process_base.lpBaseOfDll
        scan_end = base_addr + 0x20000000

        addr = base_addr
        chunk_size = 4096 * 64

        while addr < scan_end:
            try:
                data = pm.read_bytes(addr, chunk_size)
            except Exception as _:
                warn("db/decrypt.py:104 Exception", err=repr(_))
                addr += chunk_size
                continue

            for i in range(len(data) - 64):
                candidate = data[i:i+64]
                try:
                    hex_str = candidate.decode('ascii')
                    if all(c in '0123456789abcdefABCDEF' for c in hex_str):
                        test_bytes = bytes.fromhex(hex_str)
                        if len(test_bytes) == 32:
                            if not _plausible_key(test_bytes):
                                log.warning("wx_key_rejected",
                                            key_prefix=hex_str[:8] + "...",
                                            why="looks_like_pattern")
                                continue
                            log.info("wx_key_extracted", key_prefix=hex_str[:8] + "...")
                            return hex_str.lower()
                except (UnicodeDecodeError, ValueError):
                    warn("db/decrypt.py:122 (UnicodeDecodeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
                    continue

            addr += chunk_size

        log.error("wx_key_not_found_in_memory")
        return None

    except ImportError:
        log.error("pymem_not_installed", hint="pip install pymem")
        return None
    except Exception as e:
        log.error("wx_key_extract_error", error=str(e))
        return None


KDF_ITER_V4 = 256000


def _decrypt_v4_db(key_hex: str, db_path: str, out_path: str) -> bool:
    passphrase = bytes.fromhex(key_hex)

    with open(db_path, "rb") as f:
        blist = f.read()

    if len(blist) < PAGE_SZ:
        return False

    salt = blist[:SALT_SZ]
    page1 = blist[SALT_SZ:PAGE_SZ]

    enc_key = hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER_V4, 32)

    mac_salt = bytes(x ^ 0x3a for x in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, 32)
    hash_mac = hmac.new(mac_key, digestmod="sha512")
    hash_mac.update(page1[:-RESERVE_SZ + IV_SZ])
    hash_mac.update(bytes.fromhex("01 00 00 00"))
    computed = hash_mac.digest()
    stored = page1[-RESERVE_SZ + IV_SZ:][:HMAC_SZ]

    if computed != stored:
        log.debug("v4_hmac_mismatch", db=os.path.basename(db_path))
        return False

    pages = [page1]
    pages += [blist[i:i + PAGE_SZ] for i in range(PAGE_SZ, len(blist), PAGE_SZ)]

    with open(out_path, "wb") as f:
        f.write(SQLITE_HEADER)
        for page in pages:
            iv = page[-RESERVE_SZ:][:IV_SZ]
            cipher = AES.new(enc_key, AES.MODE_CBC, iv)
            f.write(cipher.decrypt(page[:-RESERVE_SZ]))
            f.write(page[-RESERVE_SZ:])

    try:
        conn = sqlite3.connect(out_path)
        conn.execute("SELECT count(*) FROM sqlite_master")
        conn.close()
        return True
    except Exception as _:
        warn("db/decrypt.py:183 Exception", err=repr(_))
        if os.path.exists(out_path):
            os.remove(out_path)
        return False


def _decrypt_v3_db(key_hex: str, db_path: str, out_path: str) -> bool:
    key = bytes.fromhex(key_hex)
    iv = b"\x00" * 16

    file_size = os.path.getsize(db_path)
    if file_size < PAGE_SZ:
        return False

    shutil.copy2(db_path, out_path)

    with open(out_path, "r+b") as f:
        header = f.read(PAGE_SZ)
        salt = header[:16]
        key_derived = hashlib.pbkdf2_hmac("sha1", key, salt, 64000, dklen=32)

        cipher = AES.new(key_derived, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(header[16:PAGE_SZ])
        new_page = salt + decrypted

        f.seek(0)
        f.write(new_page)

        for page_num in range(1, file_size // PAGE_SZ):
            offset = page_num * PAGE_SZ
            f.seek(offset)
            page = f.read(PAGE_SZ)
            cipher = AES.new(key_derived, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(page)
            f.seek(offset)
            f.write(decrypted)

    try:
        conn = sqlite3.connect(out_path)
        conn.execute("SELECT count(*) FROM sqlite_master")
        conn.close()
        return True
    except Exception as _:
        warn("db/decrypt.py:225 Exception", err=repr(_))
        if os.path.exists(out_path):
            os.remove(out_path)
        return False


def _decrypt_single_db(key_hex: str, db_path: str, out_path: str, version: int = 4) -> bool:
    if not os.path.exists(db_path):
        return False

    if version == 4:
        return _decrypt_v4_db(key_hex, db_path, out_path)
    return _decrypt_v3_db(key_hex, db_path, out_path)


def decrypt_and_merge(
    key: str,
    wx_path: str,
    out_dir: str,
    start_time: int = 0,
    end_time: int = 9999999999999,
    version: int = 4,
    incremental: bool = False,
) -> Optional[dict[str, Path]]:
    """Decrypt and merge the WeChat databases.

    With ``incremental`` each source DB is skipped when its (mtime_ns, size)
    still matches the signature recorded next to the previous decrypted copy;
    the merged output is reused when nothing changed.  The flag defaults off;
    scheduler gates it behind WX_INCREMENTAL_DECRYPT and retries a full pass
    on any failure.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    wx_root = Path(wx_path)

    msg_dbs = sorted(wx_root.rglob("message_*.db")) + sorted(wx_root.rglob("MSG*.db")) + sorted(wx_root.rglob("ChatMsg*.db"))
    # 2026-10-27：不解密 message_fts.db（微信自己的全文索引，4 MB）。
    # 全仓没有任何代码读它——我们查消息用 message_0.db / 合并后的 msg_*.db。
    # 它一天被解密 404 次，占全部解密量的 46%，纯浪费。
    msg_dbs = [p for p in msg_dbs if "fts" not in p.name.lower()]
    # 2026-10-27：跳过空壳库（实测 ...\db_storage\MSG_0.db 只有 0 字节、09-13 的残留），
    # 它每轮都解密失败 -> failed_any 恒真，（旧代码下）导致每轮都要重跑 17 MB 合并。
    def _size_ok(_p: Path) -> bool:
        try:
            return _p.stat().st_size >= 65536
        except OSError:
            return True
    msg_dbs = [p for p in msg_dbs if _size_ok(p)]
    contact_dbs = sorted(wx_root.rglob("contact.db")) + sorted(wx_root.rglob("MicroMsg.db"))
    emoticon_dbs = sorted(wx_root.rglob("emoticon.db")) + sorted(wx_root.rglob("emoticon_emoticon.db"))

    if not msg_dbs and not contact_dbs:
        log.error("no_db_found", path=wx_path)
        return None

    signatures = _load_signatures(out) if incremental else {}
    changed_any = False
    failed_any = False

    decrypted_msgs = []
    for db_file in msg_dbs:
        out_file = out / f"msg_{db_file.stem}.db"
        try:
            signature = _db_signature(db_file)
        except OSError:
            warn("db/decrypt.py:278 OSError", err=repr(__import__("sys").exc_info()[1]))
            signature = None
        if (incremental and signature is not None and out_file.exists()
                and (time.time() - out_file.stat().st_mtime) < _REFRESH_S
                and signatures.get(str(db_file)) == signature):
            decrypted_msgs.append(out_file)
            log.debug("db_decrypt_skipped", src=db_file.name)
            continue
        if _decrypt_single_db(key, str(db_file), str(out_file), version=version):
            decrypted_msgs.append(out_file)
            if signature is not None:
                signatures[str(db_file)] = signature
            changed_any = True
            log.info("db_decrypted", src=db_file.name)
        else:
            failed_any = True
            log.warning("db_decrypt_failed", src=db_file.name, out=str(out_file))

    decrypted_contacts = []
    decrypted_emoticons = []
    for db_file in contact_dbs:
        out_file = out / f"contact_{db_file.stem}.db"
        try:
            signature = _db_signature(db_file)
        except OSError:
            warn("db/decrypt.py:300 OSError", err=repr(__import__("sys").exc_info()[1]))
            signature = None
        if (incremental and signature is not None and out_file.exists()
                and (time.time() - out_file.stat().st_mtime) < _REFRESH_S
                and signatures.get(str(db_file)) == signature):
            decrypted_contacts.append(out_file)
            log.debug("db_decrypt_skipped", src=db_file.name)
            continue
        if _decrypt_single_db(key, str(db_file), str(out_file), version=version):
            decrypted_contacts.append(out_file)
            if signature is not None:
                signatures[str(db_file)] = signature
            changed_any = True
            log.info("db_decrypted", src=db_file.name)
        else:
            failed_any = True
            log.warning("db_decrypt_failed", src=db_file.name, out=str(out_file))

    for db_file in emoticon_dbs:
        out_file = out / "emoticon_emoticon.db"
        try:
            signature = _db_signature(db_file)
        except OSError:
            warn("db/decrypt.py:320 OSError", err=repr(__import__("sys").exc_info()[1]))
            signature = None
        if (incremental and signature is not None and out_file.exists()
                and (time.time() - out_file.stat().st_mtime) < _REFRESH_S
                and signatures.get(str(db_file)) == signature):
            decrypted_emoticons.append(out_file)
            log.debug("db_decrypt_skipped", src=db_file.name)
            continue
        if _decrypt_single_db(key, str(db_file), str(out_file), version=version):
            decrypted_emoticons.append(out_file)
            if signature is not None:
                signatures[str(db_file)] = signature
            changed_any = True
            log.info("db_decrypted", src=db_file.name)
        else:
            failed_any = True
            log.warning("db_decrypt_failed", src=db_file.name, out=str(out_file))

    merged_msg_path = out / "merge_msg.db"
    merged_contact_path = out / "contact.db"

    if incremental:
        _save_signatures(out, signatures)
        # 2026-10-27：failed_any 不再阻止"跳过合并"——失败的文件下一轮还会重试解密
        # （签名只在成功时写），但什么都没变时不该为它每轮重跑 17 MB 的合并。
        if not changed_any and merged_msg_path.exists():
            log.info("decrypt_merge_skipped", msg_count=len(decrypted_msgs),
                     contact_count=len(decrypted_contacts))
            return {
                "msg_db": merged_msg_path,
                "contact_db": merged_contact_path,
                "out_dir": out,
            }

    if len(decrypted_msgs) > 1:
        _merge_dbs(decrypted_msgs, str(merged_msg_path))
    elif len(decrypted_msgs) == 1:
        shutil.copy2(decrypted_msgs[0], merged_msg_path)

    if decrypted_contacts:
        shutil.copy2(decrypted_contacts[0], merged_contact_path)
    # emoticon_emoticon.db is written in place above.  Copying it onto itself
    # fails on Windows with [WinError 32] "file is in use".

    if incremental:
        _save_signatures(out, signatures)
    log.info("decrypt_merge_done", msg_count=len(decrypted_msgs), contact_count=len(decrypted_contacts))
    return {
        "msg_db": merged_msg_path,
        "contact_db": merged_contact_path,
        "out_dir": out,
    }


# 合并分片时结构不同而被跳过的表：只报一次，不刷屏。
_WARNED_TABLES: set[str] = set()


def _merge_dbs(db_paths: list[Path], out_path: str):
    shutil.copy2(db_paths[0], out_path)
    conn = sqlite3.connect(out_path)

    for db_path in db_paths[1:]:
        conn.execute(f"ATTACH DATABASE '{db_path}' AS source")
        try:
            tables = conn.execute("SELECT name FROM source.sqlite_master WHERE type='table'").fetchall()
            for (table_name,) in tables:
                if table_name.startswith("sqlite_"):
                    continue
                # 2026-10-24：FTS 影子表（message_fts_v4_0 / *_content / *_idx …）
                # 只在某个分片里存在，主库没有它 → INSERT 必失败，白刷 warn。
                # 它们是派生索引（可由 message 表重建），合并时直接跳过。
                _low = table_name.lower()
                # 2026-10-24: 图片库的 FTS 影子表(ImgFts0V0/ImgFtsAux0V0)与 table_info
                # 也是只在某个分片里存在的派生/元数据表；合并时一并跳过。
                if ("_fts" in _low or "imgfts" in _low or _low == "table_info"
                        or _low.endswith(("_content", "_data", "_idx", "_docsize",
                                        "_config", "_segdir", "_segments", "_stat"))):
                    continue
                try:
                    # 2026-10-27: 同名表在不同来源库里列不一定一样
                    # （contact.db 的 name2id 只有 username，主库是 user_name+is_session），
                    # `SELECT *` 会因列数不等整表跳过 —— 名字映射就是这么丢的。
                    # 先求列交集，只考共同的列；交集为空再退回整体拷贝。
                    _main_cols = [r[1] for r in conn.execute(
                        f"PRAGMA main.table_info([{table_name}])").fetchall()]
                    _src_cols = [r[1] for r in conn.execute(
                        f"PRAGMA source.table_info([{table_name}])").fetchall()]
                    # 列名也可能改过：contact.db 的 name2id 叫 username，主库叫 user_name。
                    # 按"同名优先、别名兜底"配对，只搬能配上的列（其余留默认值）。
                    _ALIAS = {"username": ("user_name", "userName", "UserName"),
                              "user_name": ("username", "userName", "UserName")}
                    _pairs: list[tuple[str, str]] = []      # (main 列, source 列)
                    for _mc in _main_cols:
                        if _mc in _src_cols:
                            _pairs.append((_mc, _mc))
                            continue
                        for _alt in _ALIAS.get(_mc.lower(), ()):
                            if _alt in _src_cols:
                                _pairs.append((_mc, _alt))
                                break
                    if _pairs:
                        _into = ", ".join(f"[{m}]" for m, _ in _pairs)
                        _from = ", ".join(f"[{s2}]" for _, s2 in _pairs)
                        conn.execute(
                            f"INSERT OR IGNORE INTO main.{table_name} ({_into}) "
                            f"SELECT {_from} FROM source.{table_name}")
                    else:
                        conn.execute(f"INSERT OR IGNORE INTO main.{table_name} "
                                     f"SELECT * FROM source.{table_name}")
                except Exception as _exc:  # noqa: silent(结构差异属正常：每表只报一次，见下)
                    # 2026-10-24: 同名表在不同微信版本里结构不同(name2id 列数不一致、
                    # 缺 table_info、ImgFts* 只在某个分片里)。跳过即可，但每轮每表都报太吵
                    # => 每个表名只报一次。
                    if table_name not in _WARNED_TABLES:
                        _WARNED_TABLES.add(table_name)
                        # 2026-10-24: 分片独有的表（ImgRangeV0 / ChatName2Id / SenderName2Id …）
                        # 在主库里根本不存在 ⇒ 直接放弃会把这些名字映射整表丢掉
                        # （表现为"某些联系人名字解析不出来"）。缺表就建表再拷。
                        _msg = str(_exc)
                        if _msg.startswith("no such table: main."):
                            try:
                                conn.execute(f"CREATE TABLE main.{table_name} AS SELECT * "
                                             f"FROM source.{table_name}")
                                continue
                            except Exception as _ce:  # noqa: silent(建表也失败：退回跳过)
                                warn("db/decrypt.merge_table_create_failed", table=table_name, err=repr(_ce))
                        warn("db/decrypt.merge_table_skipped", table=table_name, err=repr(_exc))
            conn.commit()
        except Exception as _:
            warn("db/decrypt.py:386 Exception", err=repr(_))
        conn.execute("DETACH DATABASE source")

    conn.close()


def detect_wx_version(db_path: str) -> int:
    if "xwechat_files" in db_path or "db_storage" in db_path:
        return 4
    if "WeChat Files" in db_path:
        return 3
    return 4
