# world.db —— 鲸鲸的「世界账本」

> 2026-10-28 建立。这份文档解释三件事：**谁在说话、说过什么、发出去到底送达没有**。

## 一句话模型

她只有三本账：

| 账本 | 文件 | 管什么 | 谁写 |
|---|---|---|---|
| **世界** | `data/world.db` | 她看到的每一条消息 + 她发出的每一条（含图） | 同步管线 / 发送链 |
| **小本本** | `data/loop/notes.jsonl` | 她写给自己的日记 + 记着的事（事实/规矩/承诺） | `loop/notes.py`（读时合并 `self_history.jsonl` + `memo.jsonl`） |
| **账** | `data/loop/ledger-YYYY-MM-DD.jsonl` | 她这一天的动作与开口（含被拦的原因） | `loop/ledger.py` |

`world.db` 是**唯一**能回答「这句话到底是不是我说的」的地方。

## schema

```sql
inbound_event(id, platform, account_id, chat_id, msg_id, ts_utc, who, who_id,
              kind, text, media_md5, media_path, quote_id, quote_user,
              is_self, src, unique(platform, account_id, msg_id))

outbound_receipt(id, turn_id, seq, platform, account_id, chat_id, text, text_hash,
                 sent_at_utc, method, ack, msg_id, confirmed_at,
                 unique(turn_id, seq))

meta(k, v)   -- 含 installed_at
```

- `is_self=1` = **这个号**发出的（可能她发的，也可能是主人手动用她号发的）
- `method` 四种：`ledger`（引擎 say）/ `wechat_gui`（GUI 直发）/ `wechat_image`（画图）/ `dbg`+`smoke`（调试）
- `ack`：`unconfirmed` → `confirmed`（查到 `msg_id` 才算真送达）

## 时序：「发出去」≠「送达」

**三条发送路径，同一个模式**：立刻记 `unconfirmed`，后台退避回查，查到才写 `confirmed`。

| 路径 | 触发 | 回查依据 | 退避 |
|---|---|---|---|
| 引擎说话 `ports.say` | 每拍 | `messages.content` 去空格后逐字比对 | 20/40/80/160/300 秒 |
| GUI 直发 `send_and_verify` | 手动/测试 | 同上（`_gui_self_proof`） | 20/40/80/160/300 秒 |
| 画图 `ports.show` | show 工具 | `message_media.kind='image'` 同会话 360 秒内新增 | 15/30/60/120/240 秒 |

**为什么必须这样**：微信写库有分钟级延迟（实测 1–300 秒不等）。`send_and_verify` 返回 `unconfirmed` 是诚实的——那一刻只是"按了回车"。

> 教训：2026-10-27 我把 `unconfirmed` 当失败重发，造成同一条消息发两遍。

## 归属判定：这句是我说的吗

`loop/snapshot.py::_mark_aliens_world()`

```python
hit = world_db.by_text(chat_id, text, window_s=180)
if hit:  keep is_self=True        # 账本里有，是我
else:    is_self=False; speaker="这个号"   # 查不到，标成"这个号"
```

- **默认怀疑**：没有回执就不认。
- 旧的「±90 秒时间窗」判定已于 2026-10-28 删除——它会在没有 ledger 记录时把她的消息误判成「这个号」（少算她）。复核：27/27 分歧行都能在 `outbound_receipt` 里按 `text_hash` 找到回执。

## 常用查询

```python
from db import world_db as w

w.freshness()                      # {"newest_utc","age_s","n_in","n_pending"}
w.by_text(chat_id, text, 180)      # 这句是不是我发的（返回回执行或 None）
w.recent_outbound(limit=50)        # 我最近说了什么
w.sense_rows(db_path, since_hours=48, limit=400)   # 读口（引擎用）
w.confirm_outbound(chat_id, text, msg_id=..., ack="confirmed")
w.sync_accounts(limit_per_account=4000, throttle_s=2.0)
```

```sql
-- 今天她说过什么（含未确认）
select sent_at_utc, chat_id, text, method, ack from outbound_receipt
where sent_at_utc >= date('now') order by id desc limit 30;

-- 手上还没送达的
select id, chat_id, text, method from outbound_receipt
where ifnull(ack,'')='' or ack='unconfirmed';

-- 谁在跟她说话（排除自己）
select chat_id, who, text, ts_utc from inbound_event
where is_self=0 order by ts_utc desc limit 20;
```

## 读口约定

- 引擎：`ports.sense_sync(path, ...)` / `ports.recent_in_chat(...)` **只读 world.db**（`YINYUE_SENSE_FROM_WORLD` 已固化）。
- 面板 `console/collector.py`：**仍直读 `account.db`**（未统一，待办）。
- `ports._account_db()` 指账号库（`send_log` 2,618 行）；`data/yinyue_meta.db` 的 `send_log` 是 0 行——**查"她发过什么"要查账号库或 world.db**。

## 已知缺口

1. `WxSender.send_image/send_file` 的**直接调用**（绕过 `ports.show`）不记账。
2. 面板侧未统一到 world.db。
3. `by_text` 的 180 秒窗口对"隔很久才发同样的句子"会漏——极端情况下会把旧消息误标成「这个号」。
4. 历史 27 条 `filehelper` 行（2026-10-27 18:26 那批）没有回执，永远补不回来（记账功能上线前的）。

## 变更史

| 日期 | 改动 | 备份后缀 |
|---|---|---|
| 10-28 | 建 `db/world_db.py`（三表 + API） | — |
| 10-28 | `to_utc` 修 `%f` 分支，回填 25,814 条 `ts_utc` | — |
| 10-28 | `sync_accounts` 改 `order by created_at desc`；`is_self` 也入库 | `.bak-orderby` `.bak-isself` |
| 10-28 | `ports.sense_sync` 只读 world.db | `.bak-rmoldpath` |
| 10-28 | 归属判定改 text_hash，删旧 ±90s 时间窗 | — |
| 10-28 | `wechat_send` 末尾加 wrapper（GUI 记账 + 异步追认） | `.bak-guisend` |
| 10-28 | `ports.show` 记 `wechat_image` + 图片追认 | `.bak-imgreceipt` |


## 文件类消息的追认（2026-10-28 补）

最容易踩的坑：**`message_media` 里永远没有 `kind='file'` 的行**
（实测分布：emoji 3185 / image 1284 / quote_image 303 / file **0**）。
微信只给图片、表情、引用图建 media 行。文件的证据在 `messages`：

```sql
select id from messages
where msg_type_name = 'file'
  and content = ?          -- 只有文件名，不是完整路径
  and chat_id = ?          -- 必须带，否则会认错会话
  and created_at >= datetime('now','localtime','-300 seconds')
order by created_at desc limit 1
```

三个注意点：

1. `content` 只有文件名 → 账本里的 `文件：xxx` 与它对不上，`world_db.by_text()` 会同时试
   原文与 `"文件：" + 原文` 两种 hash。
2. **自己发出的文件 `is_self = 0`**（微信不认它属于本号）→ 文件类消息不能靠 `is_self`
   判归属，只能靠 `outbound_receipt.by_text()` 命中。
3. `messages.id` 不是时间序（文件消息 id 是服务端大整数，比普通消息小）→ 一律
   `order by created_at`，不要 `order by id desc`。

实现：`core/execution/wx_sender.py` 末尾的 `_prove_file()` / `_confirm_file_later()`
（覆盖了旧版查 `message_media` 的实现），退避 `(15, 30, 60, 120, 240)` 秒。

通用纪律：`outbound_receipt.chat_id` 必须存**真 chat_id**，不能存显示名；解析统一放在
`world_db.record_outbound()` 入口（`chat_alias.key_of`），解析不到才原样存。

## 她能发文件吗（2026-10-28）

能。工具还是 `show`，加 `path` 参数：给 `text` 是画图，给 `path` 是发磁盘上已有的文件。
边界在 `loop/ports.py:show_file()`：只允许 `data/artifacts`、`data/persona`、`data/exports`、
`docs`、`output` 五个目录，单文件 ≤ 25 MB，超限返回 `path_not_allowed` / `file_too_big`。
