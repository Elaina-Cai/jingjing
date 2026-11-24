# 资产分区与开源边界（2026-10-26）

这份文档只回答一个问题：**哪些文件可以跟着代码一起开源，哪些永远不能。**

## 一、三个区（“能不能发”写在路径里）

```
data/persona/assets/
  private/<slug>/<kind>/     ← 第三方 / 有版权 / 作者禁止再分发：不许进仓库、不许进角色包
  <slug>/<kind>/             ← 可随包分发：自己画的、CC0、明确授权的
data/models/<engine>/        ← 大件权重（rvc / cosyvoice3 / silk / gpt_sovits），跟角色无关
```

- `kind` 目前是 `faces`（形象）与 `voice`（音色）。
- 解析只走一个入口：`core/assets/paths.py`。**任何模块都不许再自己拼 `data/persona/assets`**。
- 环境变量：`YINYUE_ASSET_ROOT` 改资产根，`YINYUE_MODEL_ROOT` 改模型根。开源用户把它们指到自己的目录即可，私有资产默认路径根本扫不到。

## 二、每个子包必须写 `license` 块

`<kind>/pack.json` 里：

```json
"license": {
  "redistribute": false,
  "commercial": false,
  "source": "来源链接或说明",
  "author": "原作者",
  "note": "自用随意，不可商用，不可再分发"
}
```

规则（`core/assets/paths.py` 的 `export_blockers`）：

| 情况 | 判定 |
|---|---|
| `redistribute: true` | 可以随角色包导出 |
| `redistribute: false` | 拒绝导出 |
| **没有 license 块** | 同样拒绝导出（fail closed，默认不安全就当不安全） |

## 三、导出闸门

`core/identity/pack_zip.py::export_pack_bytes(slug, stub=False)`

- 默认：只要有一个子包不可再分发，**整包拒绝**，返回原因 `不可导出（许可禁止再分发）：faces(license_forbids)`。
- `stub=True`：跳过该子包的实体文件，只写一个占位 `REQUIRES_USER_SUPPLY.json`（含来源与许可），并在 README 说明“需自行准备”。

**不要静默剔除**——静默剔除会让人误以为已经处理好了，这正是将来出事的方式。

## 四、三道机器闸门（不靠记性）

1. `tests/test_repo_hygiene.py`：git 跟踪的文件里不许出现 `.pth/.pt/.ckpt/.safetensors/.index/.wav/.mp3`，单文件不得超过 2MB（白名单除外），并且断言 `data/models/**`、`data/persona/assets/private/**` 确实被忽略。
2. `tests/test_pack_license.py`：不可再分发的包必须被拒绝；`stub=True` 时导出的 zip 里不能有实体图/音。
3. `scripts/release_check.py`：打 tag 前跑一遍——扫 git 跟踪文件 + 列当前所有包的许可状态 + 生成 `NOTICE.md` 的署名草稿。

## 五、新增一份资产的检查清单

1. 来源是谁、许可怎么说（能不能再分发、能不能商用）？
2. 放哪个区：`redistribute: true` → `<slug>/`；否则 → `private/<slug>/`。
3. 模型权重放 `data/models/<engine>/`，旁边写 `provenance.json`（source / file / sha256 / license / downloaded_at），照抄上游 `FeiyuVoice-*` 的做法。
4. 在 `pack.json` 写 `license` 块。
5. 跑 `pytest tests/test_repo_hygiene.py tests/test_pack_license.py`。

## 六、已知边界

- `.gitignore:17` 是 `/data/*`，所以 `data/` 整体不入库；但这**不保护你导出角色包**，导出走的是 `pack_zip`，闸门在第三步。
- **不要用 git-lfs**：LFS 会把私有资产照样推到远端。
- 鲸鲸当前的 `faces`（用户提供的公开二创立绘）与 `voice`（大肥鱼声库）都已标 `redistribute: false`。
