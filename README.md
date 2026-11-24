# 鲸鲸 · yinyue

一个**拟人化的社交智能体**：常驻 Windows，接微信（PC 4.x）与 QQ（NapCat），
自己看消息、判断该不该回、回忆相关记忆、按人设生成回复，并把动作真正发出去。

不是问答机器人 —— 它的核心难点在**"要不要开口、对谁开口、拿什么开口"**，
而不是"怎么回答"。

- 运行平台：**Windows 10/11**（微信侧走 UI 自动化，无跨平台方案）
- Python：**3.12+**
- 常驻形态：调度器进程 + 本地 Web 控制台（默认 `http://127.0.0.1:8980`（端口取自 `.env` 的 `WEB_PORT`））

---

## 快速开始

### 先决条件

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11 |
| Python | 3.12 或更高（安装时勾 `Add python.exe to PATH`） |
| 微信 | PC 微信 **4.x**，登录「鲸鲸」那个号 |
| Ollama | 建议装（快路径 / 降级用），`ollama pull qwen2.5:7b` |
| 权限 | 首次提取微信数据库密钥需要**管理员** |

### 一键安装（推荐）

1. 装好上表依赖；
2. 双击 **`安装鲸鲸.bat`**（= `scripts/setup_wizard.py`），按向导 7 步走：
   查 Python → 装依赖 → 生成 `.env` → 填 API key → 打开发送开关 → 查 Ollama → 跑体检；
3. 向导最后可勾「现在启动」，会以管理员运行 `python start.py` 完成微信密钥提取。

向导参数：`--yes`（无人值守）｜`--mirror`（清华镜像）｜`--api-key sk-xxx`｜
`--skip-install`｜`--skip-ollama`｜`--start` / `--no-start`

### 手动安装

```powershell
cd <项目目录>
pip install -r requirements.txt
copy .env.example .env
# 编辑 .env：至少填 DEEPSEEK_API_KEY + WECHAT_SEND_ENABLED=true
python start.py        # 需管理员：提微信密钥
```

依赖分四层，按需安装：

| 文件 | 内容 | 何时装 |
|---|---|---|
| `requirements.txt` | 核心运行时（含 Windows 专属） | **必装**（可编辑，只写版本下限） |
| `requirements.lock` | 核心依赖的**传递闭包**精确版本（112 个包，自动生成） | **要可复现就装这个** |
| `requirements-optional.txt` | 可选增强：语音转写（`openai-whisper`）、桌面通知 | 按需。注意 whisper 会拉进约 2.5GB 的 CUDA 版 torch |
| `requirements-dev.txt` | `pytest` / `ruff` | 只在开发时 |

核心依赖缺失会直接起不来；可选依赖缺失时代码里都有 `try/except` 守卫，只降级对应能力。

**关于 `requirements.lock`**：它保证换机器/重建环境时装出**完全一致**的版本组合。

```powershell
pip install -r requirements.lock                      # 可复现安装
python scripts\gen_lock.py                            # 改了依赖后重新生成
python scripts\gen_lock.py --check                    # 校验锁文件是否过期
```

它是**生成物，不要手改**；要改依赖请改 `requirements.txt` 再重跑生成器。

> ⚠️ 不要用 `pip freeze > requirements.lock` 代替生成器：本机环境混着整套语音训练
> 工具链（`demucs` / `bigvgan` / `ctranslate2` 等），全量冻结会把 378 个包都写进去。
> 生成器只算直接依赖的传递闭包，得 112 个。

### 日常启动

双击 **`鲸鲸启动器.pyw`** → 点「启动鲸鲸」（或直接双击 `autostart.bat`）。

命令行入口：

```powershell
python run.py scheduler   # 调度器 + Web（默认）
python run.py web         # 只起 Web 控制台
python run.py sync        # 手动同步一次微信消息
python run.py digest      # 生成今日日报
```

### 验收（四项全绿才算装好）

- [ ] 日志出现 `scheduler_running`，且 `netstat -ano | findstr :8972` 有 LISTENING
- [ ] 双击 `体检鲸鲸.bat` 无 `[XX]` 项
- [ ] `http://127.0.0.1:8972/chat` 打得开
- [ ] 用主人号发一条消息，鲸鲸有反应，日志可见 `route_will_reply`

---

## 目录结构

```
run.py                 统一入口：scheduler / web / sync / digest
start.py               首次启动：检测账号 → 提微信密钥 → 写 .env → 起调度器
安装鲸鲸.bat           一键安装向导（scripts/setup_wizard.py）
鲸鲸启动器.pyw         桌面控制台：启动/停止/体检/平台接入（tkinter + 托盘）
体检鲸鲸.bat           环境体检（scripts/doctor.py）
平台接入.bat           多平台接入统一入口（微信 / QQ）

core/                  核心域（21 个子包，见下）
  ├ terminal/          事件流「一等公民」+ 说话人归属 + ACL
  ├ routing/           路由：这条消息要不要回、由谁回
  ├ conversation/      对话理解：收件人判定、影子观察、报告
  ├ context/           上下文装配与裁剪（房间状态、预算）
  ├ memory/            记忆召回与写入
  ├ affect/            情绪状态
  ├ people/            人物 / 联系人认知
  ├ identity/          身份与账号
  ├ execution/         执行层：微信 UI 自动化（发送/表情/视觉）
  ├ ingestion/         数据接入（解密、解析、入库）
  ├ analysis/          事件读侧分析
  ├ evidence/          跨账号消息索引与游标
  ├ speech/            语音合成与出站
  ├ quality/           质量与校准
  ├ observability/     决策 trace、看板
  ├ infra/  config/  contracts/  data/  preprocess/
agents/                智能体：主对话、群聊、私聊、记忆、规划、自进化…
web/                   FastAPI 控制台（/chat /settings /memory /setup /accounts）
db/                    SQLAlchemy 模型 + 仓储 + 迁移（migrations.py）
llm/                   LLM 客户端、provider 注册表、视觉能力探测
scheduler/             常驻调度器：消息主循环 + 定时任务
notify/                系统通知
scripts/               运维与工具脚本（安装/体检/备份/自检）
tests/                 回归测试（见下）
tools/                 外部二进制（DbkeyHook、Tcl 补丁树等）
configs/               场景集与策略配置（JSON）
```

---

## 核心概念

理解这几条，代码基本就能读了。

### 1. 事件流是唯一权威

`terminal_events` 表是「谁在什么时候说了什么」的**唯一真相**。所有理解、记忆、回复
都从它读。所有写入都归一化到它。

**说话人归属**只有一处权威解析：`core/terminal/speaker.py::resolve_speaker`。
不要在别处再写一套「判断这句是谁说的」。

### 2. actor 语义（自产事件）

鲸鲸自己产出的 reply / thought / action 事件：

- `actor_person_id` = **bot 自己的账号**（不是对话对方）
- 对方放 `recipient_person_ids`

这条不变量由 `scripts/gate_hard_rules.py::check_actor_semantics` 锁死；
私聊可见性 ACL 按 recipient 授予。改这块前先跑门禁。

### 3. 记忆双轨

| | `memory_projections` | `person_notes` |
|---|---|---|
| 用途 | **主动回忆**（按需检索） | **每轮注入**（常在上下文） |
| 失效 | `valid_to` 置上 = 已失效 | — |

`valid_to` 一旦置上，召回和门禁**都必须忽略**。

### 4. 两扇闸门，只做减法

系统层判定「不接」**不可被模型覆盖**；系统放行后，模型仍可输出 `[SILENT]` 选择沉默。
这是「不该说话就不说话」的实现方式，不要为了「更爱说话」把它拆掉。

### 5. 人设与身份口径

人设见 `core` 内 persona 配置（原设计稿见 git 历史）。被问「你是不是 AI / 人机」时：
**不承认、不否认、不编人类身份**，用 persona 口吻俏皮带过。
不要新增任何声称自己是人类的输出。

### 6. 功能开关

所有重构态功能挂在 `.env` 的 `YINYUE_FEATURE_*` 上，**默认关闭**，
在影子模式下验证后才转正。改动行为类功能默认先加开关。

> ⚠️ `.env` 重复键坑：普通键（`WECHAT_*` / `DEEPSEEK_*`）**后出现的覆盖前面的**；
> `YINYUE_*` 开关**先出现的生效**。查重：
> ```powershell
> Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' } |
>   % { ($_ -split '=',2)[0] } | Group-Object | ? Count -gt 1
> ```

---

## 测试

**测试不是 pytest 套件**。每个 `tests/test_*.py` 是独立可执行脚本，
约定为 `def main() -> bool` + `raise SystemExit(0 if main() else 1)`。

跑全部：

```powershell
cd <项目目录>
python scripts\run_all_tests.py                    # 全量
python scripts\run_all_tests.py --filter terminal  # 按文件名过滤
python scripts\run_all_tests.py --timeout 600
```

单跑一个：

```powershell
python tests\test_wx_vision.py     # 微信感知与发送离线回归（不用开微信）
```

其它自检：

```powershell
python scripts\check_layering.py                    # 分层/依赖规则
python scripts\gate_hard_rules.py                   # 硬门禁（actor 语义等）
python -m core.execution.wx_diagnose status --activate   # 微信自动化自检
```

**测试隔离**：跑测试前需要设环境变量给日志/trace 改道，否则夹具会污染真实日志：

```powershell
$env:CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD = 100000
$env:YINYUE_MEMORY_RECALL_TRACE_PATH = "<临时目录>\memory_recall_trace.jsonl"
$env:YINYUE_ADMISSION_TRACE_PATH     = "<临时目录>\admission_trace.jsonl"
```

`scripts/run_all_tests.py` 已内置上述改道，直接跑它即可。

> ⚠️ **不要用 pytest 跑 `tests/`。** 两个原因：
> ① pytest 不会做上面的日志/trace 改道，夹具会污染生产 trace；
> ② pytest 会**忽略测试函数的返回值**——本项目用 `return False` 表示失败，
> 被 pytest 收集后失败会被记成 PASSED（假绿）。
>
> 因此 `pyproject.toml` 已把 pytest 配置为**不收集任何用例**，
> 并把 `PytestReturnNotNoneWarning` 升级为错误作为兜底。
> 详见 `pyproject.toml` 中 `[tool.pytest.ini_options]` 上方的说明。

---

## 静态检查

用 `ruff`（装在 `requirements-dev.txt`）：

```powershell
python -m ruff check .                 # 报告问题
python -m ruff check . --statistics    # 按规则看债务分布
python -m ruff check path/to/file.py   # 只看一个文件
```

规则与忽略项的理由都写在 `pyproject.toml` 的 `[tool.ruff]` 段里，改规则前先读那段。

几点说明：

- `E501`（行长）**不作为门禁**，`line-length = 100` 只用于记录规范值。
- `E402`（导入不在顶部）是既有约定：多处必须先 `sys.path.insert` / `load_dotenv`。
- `F401`（未用导入）、`F841`（未用变量）、`F811`（重复定义）**禁止批量自动修**——
  本项目大量依赖「导入即注册」的副作用（运行时可见 `account_db_registered` /
  `provider_registered` / `intent_executor_registered`），删错会静默摘掉注册。
  需人工逐个确认。当前存量见上文「已知工程债」。

---

## 排障

| 现象 | 处理 |
|---|---|
| 只分析、不回复 | `.env` 里 `WECHAT_SEND_ENABLED=true`；跑 `体检鲸鲸.bat` |
| 微信密钥失效 / 解密失败 | 正常（微信 4.x 每次启动换 key）：系统自动重提取；连续失败手动跑 `tools\DbkeyHook\DbkeyHookUI.exe` 把 key 贴进 `.env` |
| 起不来，日志只有 `scheduler_already_running` | 单实例锁：先停旧实例再起 |
| `No module named 'core'` | 必须先 `cd <项目目录>` 再跑 |
| 局域网/公网访问 Web | `WEB_HOST` 非 `127.0.0.1` 时必须在 `.env` 设 `WEB_TOKEN` |
| `.env` 配了不生效 | 重复键坑，见上文「功能开关」 |
| 表情包发不出去 | 看日志 `wx_search_frame_not_wechat` 的 `reason=`；先 `python -m core.execution.wx_diagnose send --who 文件传输助手 --mood 调皮 --dry-run` 演练 |
| 视觉模型不通 | 看日志 `llm_vision_providers_skipped`；配 `DASHSCOPE_API_KEY` / `OPENAI_API_KEY`，或 `OLLAMA_MODEL` 换多模态模型 |
| QQ / NapCat 下载超时 | 默认走镜像候选链；不行手动下载 Release 后 `--napcat-dir <目录>` 指定 |
| 通知收不到 | 正常：需自建 `data/push_config.json` 配渠道 |

完整 12 项体检：`python scripts\doctor.py`，或在服务运行时打开 `http://127.0.0.1:8972/setup`。

---

## 常用操作

| 想干什么 | 入口 |
|---|---|
| 换微信号 | 停服 → 备份 → 新号登录 → `python scripts\get_wechat_info.py --set-env` → `start.py` → 验收 |
| 接 QQ | 双击 `平台接入.bat` → 选 qq → 下载 NapCat → 手机 QQ 扫码 → 重启鲸鲸 |
| 账号管理 | 服务运行时打开 `http://127.0.0.1:8972/accounts`，改完重启生效 |
| 备份 / 恢复 | `python scripts\backup_all.py`；`python scripts\restore_drill.py` 演练 |
| 开机自启 | `python install_startup.py enable/status/disable` |
| 配置主人号 | `.env` 的 `WECHAT_OWNER_WXIDS`；验证 `python scripts\authority_coverage_audit.py` |

---

## 运行约定

- **运行权归主人**：换启动方式或换代码后，**先停旧实例再起**（单实例锁会互相抢）。
- **关启动器 ≠ 停鲸鲸**：要停请点「停止」。
- 动生产数据库前先 `backups/` 备份，且确认 `python run.py scheduler` 未运行。
- 改行为 / 动历史数据前先报清影响面，能不动就不动。

---

## 已知工程债

### 已处理（2026-09-27）

- ~~版本控制未真正启用~~ → 已建 `.gitignore`（密钥、运行时数据、缓存、13G 素材均已排除）
  并完成基线提交，有了回滚点。
- ~~依赖清单不完整~~ → `requirements.txt` 已按代码实际 import 重建，并用
  `importlib.metadata` 做了模块→分发包的交叉验证（无遗漏）；
  另拆出 `requirements-optional.txt`（重/可选）与 `requirements-dev.txt`。
- ~~`tools/DbkeyHook` 是坏掉的子模块~~ → 已去子模块化，
  `DbkeyHookCMD.exe` / `DbkeyHookUI.exe` 两个关键二进制已入基线。
- ~~`pyproject.toml` 的 pytest 配置无效~~ → 原 `[tool:pytest]` 是 setup.cfg 语法，
  在 `pyproject.toml` 中从不生效；已改为 `[tool.pytest.ini_options]`。
  但**没有**简单地让 pytest 收集 `tests/` —— 实测发现那样会造成「假绿」
  （pytest 忽略测试函数返回值，`return False` 被判为 PASSED），
  且绕过了日志/trace 隔离。现改为显式不收集 + 警告升级为错误，见上文「测试」。
- ~~根目录堆积一次性脚本~~ → 已归档 447 项 / 49MB 到 `scripts/_scratch/`（移动非删除）。
  仓库根目录 **490 → 19 个文件**。
- ~~无 lint 配置~~ → 已加 ruff 配置（见 `pyproject.toml` 的 `[tool.ruff]`）。
  引入 lint 时顺带查出并修掉了 **3 个真实缺陷**（见下）。
- ~~加密库双份并存~~ → `db/image_decrypt.py` 原第 144 行用 `Crypto`（pycryptodome），
  同文件 61 行却用 `Cryptodome`（pycryptodomex），且那处是 `except Exception: pass`——
  少装一个包表情包解密会**静默失败**。已统一为 `Cryptodome`，
  并从依赖里移除 `pycryptodome`（实测两库密文一致、可互解，改动前后行为等价）。

### 引入 lint 时查出的 3 个真实缺陷（均已修）

| 位置 | 问题 | 危害 |
|---|---|---|
| `db/repo_knowledge.py:119` | 用了 `delete(KnowledgeDoc)` 却只 `import select, func` | `delete_by_source()` 一调用就 **NameError** |
| `db/migrations.py:145` | 裸 f-string 语句（SQL 构造后被丢弃），且引用未定义的 `col` | SQL **从未执行**（所谓回填从未生效）；条件若成立会 NameError 让整条迁移崩掉。当前所有库 `messages` 已无 `wxid` 列，条件恒 False，属休眠缺陷 |
| `agents/planner.py:165,168`、`scripts/migrate_to_multi_account.py` ×10 | 类型注解引用了未导入的名字 | 有 PEP 563 保护不会真炸，但注解失真、静态分析失效 |

### 尚未处理

在动手重构前先读一遍：

- **异常静默吞掉 → 已分级，待逐条处理**：全项目 `except Exception` 1200+ 处，
  其中 **610 处**整个 handler 只做 `pass` / 返回空值 / `continue`（AST 实测）。
  已用 `scripts/audit_silent_except.py` 按影响面分成 A/B/C 三级：
  **A 100 处**（吞掉后把空/假值返回给调用方）、B 367 处、C 143 处。
  **完整清单见 `docs/静默异常分级清单.md`**，重新生成：
  ```powershell
  python scripts\audit_silent_except.py           # 看汇总
  python scripts\audit_silent_except.py --write   # 重写清单
  python scripts\audit_silent_except.py --level A # 只看 A 类明细
  ```
  注意 A 类里要分两种：**fail-closed**（权限门返回 `False`，通常刻意，补日志即可）
  与**静默降级**（数据读取返回 `[]`/`None`/`""`，最危险，优先修）。
- **迁移双轨**：`alembic` 只在 `migrations/versions/` 留了一个占位，
  真实迁移是 `db/migrations.py` 里手写 DDL（51 处）+ 自定义版本链。
- **lint 存量债务 63 项**（`ruff check .` 可复现）：`E702` 同行分号 31（23 在 tests）、
  `F841` 未用变量 21、`F811` 重复定义 11。
  后两者**不能自动修** —— `F841` 的赋值可能有副作用，`F811` 涉及「导入即注册」的
  副作用导入（项目运行时可见 `account_db_registered` / `provider_registered` 等日志），
  删错会静默摘掉注册。需人工逐个确认。另 `F401`（未用导入 186 处）同理，已在
  ruff 配置中列为忽略项并注明原因。
- **无 CI**：仓库没有远程，暂不适用。
