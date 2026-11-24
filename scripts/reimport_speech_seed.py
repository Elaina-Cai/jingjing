"""把 configs/speech_seed 里的人设和规矩重新导入数据库。

背景
====
``SpeechBootstrap`` 只在数据库为空时导入种子：
- 人设只在没有已发布版本时创建；
- 同名规则已存在时不会被覆盖；
所以修改了 ``configs/speech_seed/*.json`` 之后，旧数据库不会自动更新。

这个脚本用于把种子显式同步到当前数据库：
1. 基于 ``persona_yinyue.json`` 创建一条新的 persona version（旧版本保留，可回滚）；
2. 同步更新旧 ``personas`` 表里的 system_prompt / traits / voice；
3. 按 key upsert ``policy_seed.json`` 里的所有规则（覆盖旧内容，新增缺失规则）；
4. 默认先创建一个 policy 快照，方便在 Web「高级设置 → 备份与恢复」里回滚。

用法::

    python scripts/reimport_speech_seed.py
    python scripts/reimport_speech_seed.py --persona-only
    python scripts/reimport_speech_seed.py --rules-only --no-snapshot
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.bootstrap import get_repo, get_speech_store, init_singletons  # noqa: E402
from db.write_guard import declare_writes_allowed  # noqa: E402

SEED_DIR = ROOT / "configs" / "speech_seed"


def _read_json(name: str):
    path = SEED_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _active_persona(repo):
    active = list(repo.personas.list_active())
    if active:
        return active[0]
    rows = list(repo.personas.list_all())
    if rows:
        return rows[0]
    return None


def compose_persona_prompt(identity: dict) -> str:
    """人设正文 = ``identity.self`` + ``identity.background``（后者非空时）。

    2026-10-10 修：原实现只取 ``identity.self``，把 ``identity.background`` **整个丢掉**——
    而 background 里带着它自己的使用约束（「灵界、修仙、狼族圣女、器灵等设定是你的底色，
    但只在与主人单独相处或主人主动提起时少量带过，**不主动向外人提**」）。

    实测后果（``scripts/expr_signal_report.py``，532 条真实回复）：13 条（2.4%）
    **无约束地**聊设定，私聊 12 / 群聊 1，**最晚 2026-10-08 仍在发生**。
    她不是"不听约束"，是**从来没拿到过这条约束**。

    两段都是给模型看的自然语言，按原顺序拼接即可。
    """
    parts = [str(identity.get("self") or "").strip()]
    background = str(identity.get("background") or "").strip()
    if background:
        parts.append(background)
    return "\n\n".join(part for part in parts if part)


def apply_persona(store, repo, seed: dict) -> dict:
    persona = _active_persona(repo)
    if persona is None:
        raise RuntimeError("没有可用的 persona，请先在 Web「人设管理」里创建并启用一个人设")

    identity = seed.get("identity") or {}
    traits = seed.get("traits") or {}
    voice = seed.get("voice") or {}
    scene_styles = seed.get("scene_styles") or {}
    prompt_sections = seed.get("prompt_sections") or []
    aliases = seed.get("aliases") or []
    version_name = str(seed.get("version_name") or seed.get("name") or "未命名人设")

    version = store.create_persona_version(
        int(persona.id),
        name=version_name,
        identity=identity,
        traits=traits,
        voice=voice,
        scene_styles=scene_styles,
        prompt_sections=prompt_sections,
        aliases=aliases,
        created_by="reimport_speech_seed",
    )
    repo.personas.update(
        int(persona.id),
        system_prompt=compose_persona_prompt(identity),
        traits=json.dumps(traits, ensure_ascii=False),
        voice_config=json.dumps(voice, ensure_ascii=False),
    )
    return {
        "persona_id": int(persona.id),
        "version_id": version.id,
        "version": version.version,
        "version_name": version.name,
    }


def apply_rules(store, rules: list[dict]) -> dict:
    updated = 0
    created = 0
    for rule in rules:
        key = str(rule.get("key") or "").strip()
        if not key:
            continue
        existed = store.get_rule(key) is not None
        store.upsert_rule(
            key,
            name=rule.get("name", ""),
            scope_type=rule.get("scope_type", "global"),
            scope_id=rule.get("scope_id", ""),
            platform=rule.get("platform", ""),
            account_id=rule.get("account_id", ""),
            chat_type=rule.get("chat_type", ""),
            scene=rule.get("scene", ""),
            category=rule.get("category", "style"),
            enforcement=rule.get("enforcement", "prompt"),
            effect=rule.get("effect", "allow"),
            match_type=rule.get("match_type", "always"),
            pattern=rule.get("pattern", ""),
            payload_json=json.dumps(rule.get("payload") or {}, ensure_ascii=False),
            priority=int(rule.get("priority") or 100),
            enabled=1 if rule.get("enabled", True) else 0,
            immutable=1 if rule.get("immutable") else 0,
            conditions_json=json.dumps(rule.get("conditions") or {}, ensure_ascii=False),
        )
        if existed:
            updated += 1
        else:
            created += 1
    return {"rules_updated": updated, "rules_created": created}


def main() -> int:
    parser = argparse.ArgumentParser(description="重新导入 speech seed 到数据库")
    parser.add_argument("--persona-only", action="store_true", help="只导入人设")
    parser.add_argument("--rules-only", action="store_true", help="只导入规则")
    parser.add_argument("--no-snapshot", action="store_true", help="导入前不创建策略快照")
    args = parser.parse_args()

    if args.persona_only and args.rules_only:
        parser.error("--persona-only 和 --rules-only 不能同时使用")

    # 2026-10-10 修：db.write_guard（2026-10-02 之后）要求"写生产库"的进程**显式声明**，
    # 而本脚本是改生产人设的管理入口（文档也写着改完种子跑它），却没声明 —— 实测直接
    # ProductionWriteRefused，等于那条路被守卫挡死。照 scheduler.main 的写法补上：
    # 声明写在入口，判据来自**调用方**的意图，而不是藏在写口里（那样等于谁都能写）。
    declare_writes_allowed(declared_by="scripts.reimport_speech_seed")
    # 迁移权限**不**在此声明：本脚本的职责是导人设/规则，不是把生产 schema 迁上去。
    # 库有 pending 时按 125513b 的设计拒绝（维护脚本应先把服务起起来做迁移）；
    # 库已最新时迁移门按"没有 pending"自行放行，不需要 allow_production=True。
    init_singletons()
    repo = get_repo()
    store = get_speech_store()

    report: dict = {}
    if not args.no_snapshot:
        snapshot = store.create_policy_version(
            name="reimport_speech_seed 之前",
            changed_by="reimport_speech_seed",
        )
        report["snapshot_version"] = snapshot.version

    if not args.rules_only:
        report.update(apply_persona(store, repo, _read_json("persona_yinyue.json")))
    if not args.persona_only:
        report.update(apply_rules(store, _read_json("policy_seed.json")))

    store.invalidate()
    store.bump_revision("reimport_speech_seed", "script", report)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
