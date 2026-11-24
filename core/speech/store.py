from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from db import meta_models as mm
from safelog import warn

# 闸门配置行在 ``policy_rules`` 里的 key（数据通道契约）：key 等于它、或以
# ``output_gate_`` / ``output_gate.`` 开头的数据行都会被 ``output_gate_payloads``
# 收进 ``GateConfig.from_payloads``。改数据即改阈值，不用重启（读 revision）。
OUTPUT_GATE_RULE_KEY = "output_gate"


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        warn("core/speech/store.py:26 Exception", err=repr(__import__("sys").exc_info()[1]))
        return default


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        warn("core/speech/store.py:35 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


class SpeechStore:
    """Persistence facade for the speech subsystem.

    The class deliberately has no FastAPI / agent imports so it can be reused
    by the scheduler, CLI maintenance scripts and tests.
    """

    def __init__(self, engine):
        self.engine = engine
        self._Session = sessionmaker(bind=engine, expire_on_commit=False)
        self._revision_cache: str | None = None
        self._revision_checked_at: float = 0.0
        self._cache_revision: str | None = None
        self._rules_cache: list = []
        self._profiles_cache: list = []
        self._profile_bindings_cache: list = []
        self._profile_rules_cache: list = []
        self._lexicons_cache: list = []

    # ------------------------------------------------------------------
    # revisions / cache invalidation
    # ------------------------------------------------------------------
    def get_revision(self) -> str:
        now = time.time()
        if self._revision_cache and now - self._revision_checked_at < 1.0:
            return self._revision_cache
        with self._Session() as s:
            row = s.execute(
                select(mm.SpeechConfigRevision)
                .order_by(mm.SpeechConfigRevision.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = mm.SpeechConfigRevision(
                    revision=uuid.uuid4().hex,
                    changed_by="system",
                    change_type="bootstrap",
                    detail_json=_dumps({"reason": "initial revision"}),
                )
                s.add(row)
                s.commit()
                s.refresh(row)
            self._revision_cache = row.revision
            self._revision_checked_at = now
            return row.revision

    def bump_revision(self, change_type: str = "manual", changed_by: str = "system",
                      detail: dict[str, Any] | None = None) -> str:
        revision = uuid.uuid4().hex
        with self._Session() as s:
            s.add(mm.SpeechConfigRevision(
                revision=revision,
                changed_by=changed_by,
                change_type=change_type,
                detail_json=_dumps(detail or {}),
            ))
            s.commit()
        self._revision_cache = revision
        self._revision_checked_at = time.time()
        return revision

    def invalidate(self) -> None:
        self._revision_cache = None

    # ------------------------------------------------------------------
    # output gate data channel (MVP-2：闸门阈值/词表"落数据不落代码")
    # ------------------------------------------------------------------
    # 读法：``_ensure_cache()`` 比对 ``speech_config_revisions`` 的最新 revision，
    # 变了才重查 ``policy_rules``（1 秒 revision 缓存，见 get_revision）。所以
    # **改一行数据（或 bump_revision）在下一个判定就生效，不用重启进程**；
    # 代码里的 ``GateConfig()`` 只是 policy_rules 里没有该行时的兜底默认值。
    def output_gate_payloads(self, key: str = OUTPUT_GATE_RULE_KEY) -> list[dict]:
        """闸门配置行：``policy_rules`` 里 enabled=1、key/payload 命中闸门的那些。

        返回 payload dict 列表（``GateConfig.from_payloads`` 的白名单键会被读取，
        其余键忽略）；读不到/解析失败返回 ``[]``——调用方回落到代码默认值。
        数据行写法举例::

            key=output_gate, payload_json={"hard_max_chars": 80, "blocked": ["串台"]}

        key 精确等于 ``output_gate`` 或以下划线/点号开头（``output_gate_*``、
        ``output_gate.*``）都算，方便按维度拆成多行数据。
        """
        wanted = str(key or "").strip()
        try:
            payloads: list[dict] = []
            for rule in self.list_rules():
                rule_key = str(getattr(rule, "key", "") or "").strip()
                if rule_key != wanted and not rule_key.startswith(
                    (f"{wanted}_", f"{wanted}.")
                ):
                    continue
                payload = _loads(getattr(rule, "payload_json", None), None)
                if isinstance(payload, dict):
                    payloads.append(payload)
            return payloads
        except Exception:  # noqa: BLE001 - 配置读不到就用代码常量，绝不挡发送
            warn("core/speech/store.py:135 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []

    def claims_for(self, chat_id: str, fields: Iterable[str] = (), *,
                   platform: str = "", account_id: str = "") -> list[Any]:
        """这个会话的 claim 台账行（M2-4；**只读**，供闸门接缝用）。

        为什么这个读入口在 store 而不是闸门里：闸门是**纯函数**（``run_output_gate``
        体内不许有 IO，``tests/test_output_gate.py`` 有结构守卫），台账是库读。所以
        读在 store、结论经 ``guards.claim_ledger_inputs`` 折进 ``GateConfig``。

        **flag 门在接缝那一侧**：本方法只负责"读得到就读"，调用方
        （``claim_ledger_inputs``）在 ``YINYUE_FEATURE_CLAIM_LEDGER=off`` 时**根本不会
        调到这里**——那正是"``off`` 时不读写"的实现点。

        ``fields`` 非空时只取这些字段（闸门只关心本回合断言到的字段）；读不到
        （表不存在 / 查询失败）一律返回空列表，**绝不因为台账读不到而拒绝服务**。
        """
        target = str(chat_id or "").strip()
        if not target:
            return []
        wanted = {str(item or "") for item in (fields or ()) if str(item or "")}
        try:
            with self._Session() as session:
                rows = list(session.execute(
                    select(mm.ClaimLedgerRow)
                    .where(mm.ClaimLedgerRow.chat_id == target)
                    .order_by(mm.ClaimLedgerRow.id)
                ).scalars().all())
        except Exception:  # noqa: BLE001 - 没有台账 = 没有支撑，不许挡发送
            warn("core/speech/store.py:164 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []
        return [
            row for row in rows
            if (not platform or row.platform in ("", str(platform)))
            and (not account_id or row.account_id in ("", str(account_id)))
            and (not wanted or row.field_key in wanted)
        ]

    # ------------------------------------------------------------------
    # persona versions
    # ------------------------------------------------------------------
    def get_latest_persona_version(self, persona_id: int) -> mm.PersonaVersion | None:
        with self._Session() as s:
            return s.execute(
                select(mm.PersonaVersion)
                .where(mm.PersonaVersion.persona_id == persona_id)
                .order_by(mm.PersonaVersion.version.desc(), mm.PersonaVersion.id.desc())
                .limit(1)
            ).scalar_one_or_none()

    def get_persona_version(self, persona_id: int, version: int) -> mm.PersonaVersion | None:
        with self._Session() as s:
            return s.execute(
                select(mm.PersonaVersion).where(
                    mm.PersonaVersion.persona_id == persona_id,
                    mm.PersonaVersion.version == version,
                ).limit(1)
            ).scalar_one_or_none()

    def list_persona_versions(self, persona_id: int | None = None) -> list[mm.PersonaVersion]:
        with self._Session() as s:
            q = select(mm.PersonaVersion).order_by(
                mm.PersonaVersion.persona_id, mm.PersonaVersion.version.desc()
            )
            if persona_id is not None:
                q = q.where(mm.PersonaVersion.persona_id == persona_id)
            return list(s.execute(q).scalars().all())

    def create_persona_version(
        self,
        persona_id: int,
        *,
        name: str,
        identity: dict[str, Any] | None = None,
        traits: dict[str, Any] | None = None,
        voice: dict[str, Any] | None = None,
        scene_styles: dict[str, Any] | None = None,
        prompt_sections: list[dict[str, Any]] | None = None,
        aliases: list[str] | None = None,
        created_by: str = "system",
    ) -> mm.PersonaVersion:
        latest = self.get_latest_persona_version(persona_id)
        next_version = (latest.version + 1) if latest else 1
        row = mm.PersonaVersion(
            persona_id=persona_id,
            version=next_version,
            name=name,
            aliases_json=_dumps(aliases or []),
            identity_json=_dumps(identity or {}),
            traits_json=_dumps(traits or {}),
            voice_json=_dumps(voice or {}),
            scene_styles_json=_dumps(scene_styles or {}),
            prompt_sections_json=_dumps(prompt_sections or []),
            created_by=created_by,
            created_at=datetime.now(),
        )
        with self._Session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
        self.bump_revision(
            change_type="persona_version",
            changed_by=created_by,
            detail={"persona_id": persona_id, "version": next_version},
        )
        return row

    def upsert_persona_version_from_persona(self, persona: Any,
                                            seed: dict[str, Any] | None = None,
                                            created_by: str = "system") -> mm.PersonaVersion:
        """Create a version from a legacy Persona row if none exists yet."""
        existing = self.get_latest_persona_version(persona.id)
        if existing is not None and seed is None:
            return existing
        seed = seed or {}
        identity = dict(seed.get("identity") or {})
        if not identity:
            identity = {"self": getattr(persona, "system_prompt", "") or ""}
        elif not identity.get("self"):
            identity["self"] = getattr(persona, "system_prompt", "") or ""
        traits = dict(seed.get("traits") or _loads(getattr(persona, "traits", None), {}))
        voice = dict(seed.get("voice") or _loads(getattr(persona, "voice_config", None), {}))
        scene_styles = dict(seed.get("scene_styles") or {
            "private": {},
            "group": {},
        })
        prompt_sections = list(seed.get("prompt_sections") or [])
        aliases = list(seed.get("aliases") or [])
        if not aliases:
            aliases = [getattr(persona, "name", "") or ""]
            aliases = [a for a in aliases if a]
        if existing is not None:
            # A seed refresh should be explicit; don't overwrite an already
            # published version silently.
            return existing
        return self.create_persona_version(
            persona.id,
            name=getattr(persona, "name", "") or "默认人设",
            identity=identity,
            traits=traits,
            voice=voice,
            scene_styles=scene_styles,
            prompt_sections=prompt_sections,
            aliases=aliases,
            created_by=created_by,
        )

    # ------------------------------------------------------------------
    # bindings
    # ------------------------------------------------------------------
    def list_bindings(self) -> list[mm.SpeechBinding]:
        with self._Session() as s:
            return list(s.execute(
                select(mm.SpeechBinding).order_by(
                    mm.SpeechBinding.priority.desc(), mm.SpeechBinding.id.desc()
                )
            ).scalars().all())

    def find_binding(self, scope_type: str, scope_id: str = "", *,
                     platform: str = "", account_id: str = "",
                     chat_type: str = "") -> mm.SpeechBinding | None:
        with self._Session() as s:
            rows = list(s.execute(
                select(mm.SpeechBinding).where(
                    mm.SpeechBinding.scope_type == scope_type,
                    mm.SpeechBinding.scope_id == scope_id,
                    mm.SpeechBinding.enabled == 1,
                ).order_by(mm.SpeechBinding.priority.desc(), mm.SpeechBinding.id.desc())
            ).scalars().all())
        for row in rows:
            if row.platform and platform and row.platform != platform:
                continue
            if row.account_id and account_id and row.account_id != account_id:
                continue
            if row.chat_type and chat_type and row.chat_type != chat_type:
                continue
            return row
        return None

    def upsert_binding(self, *, scope_type: str, scope_id: str = "",
                       platform: str = "", account_id: str = "",
                       chat_type: str = "", scene: str = "",
                       persona_id: int, persona_version: int | None = None,
                       overrides: dict[str, Any] | None = None,
                       enabled: bool = True, priority: int = 100,
                       changed_by: str = "system") -> mm.SpeechBinding:
        with self._Session() as s:
            row = s.execute(
                select(mm.SpeechBinding).where(
                    mm.SpeechBinding.scope_type == scope_type,
                    mm.SpeechBinding.scope_id == scope_id,
                    mm.SpeechBinding.platform == platform,
                    mm.SpeechBinding.account_id == account_id,
                    mm.SpeechBinding.chat_type == chat_type,
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = mm.SpeechBinding(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    platform=platform,
                    account_id=account_id,
                    chat_type=chat_type,
                )
                s.add(row)
            row.scene = scene or row.scene
            row.persona_id = persona_id
            row.persona_version = persona_version
            row.overrides_json = _dumps(overrides or {})
            row.enabled = 1 if enabled else 0
            row.priority = int(priority)
            row.updated_at = datetime.now()
            s.commit()
            s.refresh(row)
        self.bump_revision("binding", changed_by, {"scope_type": scope_type,
                                                   "scope_id": scope_id})
        return row

    def delete_binding(self, binding_id: int, changed_by: str = "system") -> bool:
        with self._Session() as s:
            row = s.get(mm.SpeechBinding, binding_id)
            if not row:
                return False
            s.delete(row)
            s.commit()
        self.bump_revision("binding_delete", changed_by, {"binding_id": binding_id})
        return True

    # ------------------------------------------------------------------
    # policy rules
    # ------------------------------------------------------------------
    def _ensure_cache(self) -> None:
        revision = self.get_revision()
        if revision == self._cache_revision:
            return
        with self._Session() as s:
            self._rules_cache = list(s.execute(
                select(mm.PolicyRule).order_by(
                    mm.PolicyRule.priority.desc(), mm.PolicyRule.id.asc()
                )
            ).scalars().all())
            self._profiles_cache = list(s.execute(
                select(mm.PolicyProfile).order_by(mm.PolicyProfile.id.asc())
            ).scalars().all())
            self._profile_bindings_cache = list(s.execute(
                select(mm.PolicyProfileBinding).order_by(
                    mm.PolicyProfileBinding.priority.desc(),
                    mm.PolicyProfileBinding.id.asc(),
                )
            ).scalars().all())
            self._profile_rules_cache = list(s.execute(
                select(mm.PolicyProfileRule).order_by(
                    mm.PolicyProfileRule.priority.desc(),
                    mm.PolicyProfileRule.id.asc(),
                )
            ).scalars().all())
            self._lexicons_cache = list(s.execute(
                select(mm.PolicyLexicon).order_by(mm.PolicyLexicon.id.asc())
            ).scalars().all())
        self._cache_revision = revision

    def list_rules(self, enabled_only: bool = True) -> list[mm.PolicyRule]:
        self._ensure_cache()
        if enabled_only:
            return [r for r in self._rules_cache if r.enabled == 1]
        return list(self._rules_cache)

    def list_profiles(self, enabled_only: bool = True) -> list[mm.PolicyProfile]:
        self._ensure_cache()
        if enabled_only:
            return [p for p in self._profiles_cache if p.enabled == 1]
        return list(self._profiles_cache)

    def list_profile_bindings(self) -> list[mm.PolicyProfileBinding]:
        self._ensure_cache()
        return [b for b in self._profile_bindings_cache if b.enabled == 1]

    def list_profile_rules(self) -> list[mm.PolicyProfileRule]:
        self._ensure_cache()
        return [r for r in self._profile_rules_cache if r.enabled == 1]

    def get_profile(self, key: str) -> mm.PolicyProfile | None:
        self._ensure_cache()
        for profile in self._profiles_cache:
            if profile.key == key:
                return profile
        return None

    def resolve_profile_keys(self, profile_scope_hits: list[tuple[str, str]]) -> list[str]:
        """Expand matched profile bindings through parent chains.

        ``profile_scope_hits`` is an ordered list of (scope_type, scope_id)
        entries from most specific to least specific.
        """
        self._ensure_cache()
        bindings = self.list_profile_bindings()
        direct: list[str] = []
        for scope_type, scope_id in profile_scope_hits:
            for binding in bindings:
                if binding.scope_type != scope_type:
                    continue
                if binding.scope_id and binding.scope_id != scope_id:
                    continue
                if binding.profile_key not in direct:
                    direct.append(binding.profile_key)
        expanded: list[str] = []
        visiting: set[str] = set()

        def add(key: str):
            if not key or key in expanded or key in visiting:
                return
            profile = self.get_profile(key)
            if profile is None:
                return
            visiting.add(key)
            if profile.parent_key:
                add(profile.parent_key)
            visiting.discard(key)
            if key not in expanded:
                expanded.append(key)

        for key in direct:
            add(key)
        return expanded

    def upsert_profile(self, key: str, **fields) -> mm.PolicyProfile:
        with self._Session() as s:
            row = s.execute(
                select(mm.PolicyProfile).where(mm.PolicyProfile.key == key).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = mm.PolicyProfile(key=key)
                s.add(row)
            for name, value in fields.items():
                if hasattr(row, name):
                    setattr(row, name, value)
            row.updated_at = datetime.now()
            s.commit()
            s.refresh(row)
        self.bump_revision("policy_profile", "system", {"key": key})
        return row

    def delete_profile(self, key: str, changed_by: str = "system") -> bool:
        with self._Session() as s:
            row = s.execute(
                select(mm.PolicyProfile).where(mm.PolicyProfile.key == key).limit(1)
            ).scalar_one_or_none()
            if not row:
                return False
            s.delete(row)
            s.commit()
        self.bump_revision("policy_profile_delete", changed_by, {"key": key})
        return True

    def upsert_profile_binding(self, profile_key: str, *, scope_type: str = "global",
                               scope_id: str = "", platform: str = "",
                               account_id: str = "", chat_type: str = "",
                               scene: str = "", priority: int = 100,
                               enabled: bool = True) -> mm.PolicyProfileBinding:
        with self._Session() as s:
            row = s.execute(
                select(mm.PolicyProfileBinding).where(
                    mm.PolicyProfileBinding.profile_key == profile_key,
                    mm.PolicyProfileBinding.scope_type == scope_type,
                    mm.PolicyProfileBinding.scope_id == scope_id,
                    mm.PolicyProfileBinding.platform == platform,
                    mm.PolicyProfileBinding.account_id == account_id,
                    mm.PolicyProfileBinding.chat_type == chat_type,
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = mm.PolicyProfileBinding(
                    profile_key=profile_key,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    platform=platform,
                    account_id=account_id,
                    chat_type=chat_type,
                )
                s.add(row)
            row.scene = scene or row.scene
            row.priority = int(priority)
            row.enabled = 1 if enabled else 0
            row.updated_at = datetime.now()
            s.commit()
            s.refresh(row)
        self.bump_revision("policy_profile_binding", "system",
                           {"profile_key": profile_key, "scope_id": scope_id})
        return row

    def set_profile_rules(self, profile_key: str, rule_keys: list[str],
                          changed_by: str = "system") -> None:
        with self._Session() as s:
            old = list(s.execute(
                select(mm.PolicyProfileRule).where(
                    mm.PolicyProfileRule.profile_key == profile_key
                )
            ).scalars().all())
            for row in old:
                s.delete(row)
            for index, rule_key in enumerate(rule_keys):
                if not rule_key:
                    continue
                s.add(mm.PolicyProfileRule(
                    profile_key=profile_key,
                    rule_key=str(rule_key),
                    priority=max(0, 100 - index),
                ))
            s.commit()
        self.bump_revision("policy_profile_rules", changed_by, {"profile_key": profile_key})

    def get_rule(self, key: str) -> mm.PolicyRule | None:
        with self._Session() as s:
            return s.execute(
                select(mm.PolicyRule).where(mm.PolicyRule.key == key).limit(1)
            ).scalar_one_or_none()

    def upsert_rule(self, key: str, **fields) -> mm.PolicyRule:
        with self._Session() as s:
            row = s.execute(
                select(mm.PolicyRule).where(mm.PolicyRule.key == key).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = mm.PolicyRule(key=key)
                s.add(row)
            for name, value in fields.items():
                if hasattr(row, name):
                    setattr(row, name, value)
            row.updated_at = datetime.now()
            s.commit()
            s.refresh(row)
        self.bump_revision("policy_rule", "system", {"key": key})
        return row

    def delete_rule(self, rule_id: int, changed_by: str = "system") -> bool:
        with self._Session() as s:
            row = s.get(mm.PolicyRule, rule_id)
            if not row:
                return False
            s.delete(row)
            s.commit()
        self.bump_revision("policy_rule_delete", changed_by, {"rule_id": rule_id})
        return True

    # ------------------------------------------------------------------
    # lexicons
    # ------------------------------------------------------------------
    def upsert_lexicon_terms(self, key: str, name: str,
                             terms: Iterable[str],
                             changed_by: str = "system") -> mm.PolicyLexicon:
        with self._Session() as s:
            lex = s.execute(
                select(mm.PolicyLexicon).where(mm.PolicyLexicon.key == key).limit(1)
            ).scalar_one_or_none()
            if lex is None:
                lex = mm.PolicyLexicon(key=key, name=name)
                s.add(lex)
                s.commit()
                s.refresh(lex)
            lex.name = name or lex.name
            for term in terms:
                term = (term or "").strip()
                if not term:
                    continue
                exists = s.execute(
                    select(mm.PolicyTerm).where(
                        mm.PolicyTerm.lexicon_id == lex.id,
                        mm.PolicyTerm.term == term,
                    ).limit(1)
                ).scalar_one_or_none()
                if exists is None:
                    s.add(mm.PolicyTerm(
                        lexicon_id=lex.id,
                        term=term,
                        match_type="contains",
                        severity="block",
                        action="regenerate_then_replace",
                    ))
            s.commit()
        self.bump_revision("lexicon", changed_by, {"key": key})
        return lex

    def list_lexicons(self) -> list[mm.PolicyLexicon]:
        with self._Session() as s:
            return list(s.execute(select(mm.PolicyLexicon)).scalars().all())

    def get_lexicon_terms(self, key: str) -> list[dict[str, Any]]:
        with self._Session() as s:
            lex = s.execute(
                select(mm.PolicyLexicon).where(mm.PolicyLexicon.key == key).limit(1)
            ).scalar_one_or_none()
            if lex is None:
                return []
            rows = list(s.execute(
                select(mm.PolicyTerm).where(
                    mm.PolicyTerm.lexicon_id == lex.id,
                    mm.PolicyTerm.enabled == 1,
                )
            ).scalars().all())
        return [
            {
                "term": r.term,
                "match_type": r.match_type,
                "severity": r.severity,
                "action": r.action,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # policy versions (snapshot / rollback)
    # ------------------------------------------------------------------
    @staticmethod
    def _row_snapshot(row, skip: set[str] | None = None) -> dict:
        skip = skip or {"id", "created_at", "updated_at"}
        data = {}
        for column in row.__table__.columns:
            name = column.name
            if name in skip:
                continue
            value = getattr(row, name, None)
            if isinstance(value, datetime):
                value = value.isoformat()
            data[name] = value
        return data

    def _policy_snapshot(self) -> dict:
        self._ensure_cache()
        return {
            "rules": [self._row_snapshot(r) for r in self.list_rules(enabled_only=False)],
            "profiles": [self._row_snapshot(p) for p in self.list_profiles(enabled_only=False)],
            "profile_bindings": [
                {
                    "profile_key": b.profile_key,
                    "scope_type": b.scope_type,
                    "scope_id": b.scope_id,
                    "platform": b.platform,
                    "account_id": b.account_id,
                    "chat_type": b.chat_type,
                    "scene": b.scene,
                    "priority": b.priority,
                    "enabled": b.enabled,
                }
                for b in self.list_profile_bindings()
            ],
            "profile_rules": [
                {
                    "profile_key": r.profile_key,
                    "rule_key": r.rule_key,
                    "priority": r.priority,
                    "enabled": r.enabled,
                }
                for r in self.list_profile_rules()
            ],
        }

    def create_policy_version(self, name: str = "", changed_by: str = "system") -> mm.PolicyVersion:
        snapshot = self._policy_snapshot()
        with self._Session() as s:
            current = s.execute(
                select(mm.PolicyVersion).order_by(mm.PolicyVersion.version.desc()).limit(1)
            ).scalar_one_or_none()
            next_version = (current.version + 1) if current else 1
            row = mm.PolicyVersion(
                version=next_version,
                name=name or f"policy-v{next_version}",
                snapshot_json=_dumps(snapshot),
                created_by=changed_by,
                created_at=datetime.now(),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
        return row

    def list_policy_versions(self) -> list[mm.PolicyVersion]:
        with self._Session() as s:
            return list(s.execute(
                select(mm.PolicyVersion).order_by(mm.PolicyVersion.version.desc())
            ).scalars().all())

    def get_policy_version(self, version_id: int) -> mm.PolicyVersion | None:
        with self._Session() as s:
            return s.get(mm.PolicyVersion, version_id)

    def diff_policy_version(self, version_id: int) -> dict:
        row = self.get_policy_version(version_id)
        if row is None:
            return {}
        old = _loads(row.snapshot_json, {}) or {}
        new = self._policy_snapshot()
        result = {"added": [], "removed": [], "changed": []}

        for section in ("rules", "profiles", "profile_bindings", "profile_rules"):
            old_items = old.get(section) or []
            new_items = new.get(section) or []
            key_name = "key" if section in ("rules", "profiles") else (
                "rule_key" if section == "profile_rules" else "scope_id"
            )
            old_map = {str(item.get(key_name) or i): item for i, item in enumerate(old_items)}
            new_map = {str(item.get(key_name) or i): item for i, item in enumerate(new_items)}
            for key in sorted(set(old_map) | set(new_map)):
                if key not in old_map:
                    result["added"].append({"section": section, "key": key})
                elif key not in new_map:
                    result["removed"].append({"section": section, "key": key})
                elif old_map[key] != new_map[key]:
                    result["changed"].append({"section": section, "key": key,
                                              "before": old_map[key],
                                              "after": new_map[key]})
        return result

    def restore_policy_version(self, version_id: int,
                               changed_by: str = "system") -> bool:
        row = self.get_policy_version(version_id)
        if row is None:
            return False
        snapshot = _loads(row.snapshot_json, None)
        if not isinstance(snapshot, dict):
            return False
        with self._Session() as s:
            # Replace the active conduct layer with the snapshot atomically.
            for table in (mm.PolicyProfileRule, mm.PolicyProfileBinding,
                          mm.PolicyProfile):
                for existing in s.execute(select(table)).scalars().all():
                    s.delete(existing)
            for existing in s.execute(select(mm.PolicyRule)).scalars().all():
                s.delete(existing)
            s.flush()

            for data in snapshot.get("profiles", []):
                if not data.get("key"):
                    continue
                s.add(mm.PolicyProfile(
                    key=data["key"],
                    name=data.get("name", ""),
                    description=data.get("description", ""),
                    parent_key=data.get("parent_key", ""),
                    enabled=int(data.get("enabled", 1)),
                ))
            for data in snapshot.get("profile_bindings", []):
                if not data.get("profile_key"):
                    continue
                s.add(mm.PolicyProfileBinding(
                    profile_key=data["profile_key"],
                    scope_type=data.get("scope_type", "global"),
                    scope_id=data.get("scope_id", ""),
                    platform=data.get("platform", ""),
                    account_id=data.get("account_id", ""),
                    chat_type=data.get("chat_type", ""),
                    scene=data.get("scene", ""),
                    priority=int(data.get("priority", 100)),
                    enabled=int(data.get("enabled", 1)),
                ))
            for data in snapshot.get("profile_rules", []):
                if not data.get("profile_key") or not data.get("rule_key"):
                    continue
                s.add(mm.PolicyProfileRule(
                    profile_key=data["profile_key"],
                    rule_key=data["rule_key"],
                    priority=int(data.get("priority", 0)),
                    enabled=int(data.get("enabled", 1)),
                ))
            for data in snapshot.get("rules", []):
                if not data.get("key"):
                    continue
                fields = dict(data)
                key = fields.pop("key")
                if "payload_json" not in fields and "payload" in fields:
                    fields["payload_json"] = _dumps(fields.pop("payload"))
                if "conditions_json" not in fields and "conditions" in fields:
                    fields["conditions_json"] = _dumps(fields.pop("conditions"))
                s.add(mm.PolicyRule(key=key, **fields))
            s.commit()
        self.bump_revision("policy_restore", changed_by, {"version_id": version_id})
        self._cache_revision = None
        return True

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------
    def write_audit(self, **fields) -> mm.OutboundAudit:
        row = mm.OutboundAudit(**fields)
        with self._Session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
        return row

    def audit_stats(self, limit: int = 2000) -> dict:
        with self._Session() as s:
            rows = list(s.execute(
                select(mm.OutboundAudit)
                .order_by(mm.OutboundAudit.id.desc())
                .limit(limit)
            ).scalars().all())
        decisions: dict[str, int] = {}
        rules: dict[str, int] = {}
        statuses: dict[str, int] = {}
        for row in rows:
            decisions[row.decision or ""] = decisions.get(row.decision or "", 0) + 1
            statuses[row.status or ""] = statuses.get(row.status or "", 0) + 1
            try:
                ids = __import__("json").loads(row.rule_ids_json or "[]")
            except Exception:
                warn("core/speech/store.py:839 Exception", err=repr(__import__("sys").exc_info()[1]))
                ids = []
            for rule_id in ids:
                rules[str(rule_id)] = rules.get(str(rule_id), 0) + 1
        return {
            "total": len(rows),
            "decisions": decisions,
            "statuses": statuses,
            "rules": rules,
        }

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256((content or "").encode("utf-8", errors="replace")).hexdigest()


_STORE: SpeechStore | None = None


def get_speech_store(engine=None) -> SpeechStore:
    global _STORE
    if engine is not None:
        _STORE = SpeechStore(engine)
        return _STORE
    if _STORE is None:
        try:
            from core.terminal.store_port import get_meta_repo
            _STORE = SpeechStore(get_meta_repo().engine)
        except Exception:
            raise RuntimeError("speech store is not initialised")
    return _STORE
