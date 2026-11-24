from __future__ import annotations

import json
import structlog
from pathlib import Path
from typing import Any

from core.speech.store import SpeechStore
from safelog import warn

log = structlog.get_logger()

SEED_DIR = Path(__file__).resolve().parents[2] / "configs" / "speech_seed"


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("speech_seed_read_failed", path=str(path), error=str(exc))
        return None


class SpeechBootstrap:
    """Move shipped identity/conduct data into the database once."""

    def __init__(self, store: SpeechStore, repo: Any):
        self.store = store
        self.repo = repo

    def _active_persona(self):
        try:
            active = list(self.repo.personas.list_active())
            if active:
                return active[0]
        except Exception:
            warn("core/speech/bootstrap.py:37 Exception", err=repr(__import__("sys").exc_info()[1]))
        try:
            rows = list(self.repo.personas.list_all())
            if rows:
                return rows[0]
        except Exception:
            warn("core/speech/bootstrap.py:43 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None

    def ensure_seed(self) -> dict[str, int]:
        """Returns a small report: {persona_versions, bindings, rules, lexicons}."""
        report = {"persona_versions": 0, "bindings": 0, "rules": 0, "lexicons": 0}

        persona = self._active_persona()
        seed_persona = _read_json(SEED_DIR / "persona_jingjing.json") or {}
        if persona is not None:
            existing = self.store.get_latest_persona_version(persona.id)
            if existing is None:
                try:
                    self.store.upsert_persona_version_from_persona(
                        persona, seed=seed_persona, created_by="bootstrap",
                    )
                    report["persona_versions"] += 1
                except Exception as exc:
                    log.warning("speech_persona_seed_failed",
                                persona_id=getattr(persona, "id", None),
                                error=str(exc))
        else:
            log.warning("speech_seed_no_persona",
                        hint="create/activate a persona in the Web UI first")

        persona_id = getattr(persona, "id", None) if persona is not None else None
        if persona_id and not self.store.list_bindings():
            try:
                self.store.upsert_binding(
                    scope_type="global",
                    scope_id="",
                    persona_id=int(persona_id),
                    scene="",
                    priority=0,
                    changed_by="bootstrap",
                )
                report["bindings"] += 1
            except Exception as exc:
                log.warning("speech_binding_seed_failed", error=str(exc))

        # Seed rules are imported additively: an existing rule with the same
        # key is never overwritten by a later startup, but new built-in keys
        # (for example engagement policy) are added to older databases.
        rules = _read_json(SEED_DIR / "policy_seed.json") or []
        for rule in rules:
            try:
                key = str(rule.get("key") or "").strip()
                if not key:
                    continue
                if self.store.get_rule(key) is not None:
                    continue
                self.store.upsert_rule(
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
                    payload_json=json.dumps(
                        rule.get("payload") or {}, ensure_ascii=False,
                    ),
                    priority=int(rule.get("priority") or 100),
                    enabled=1 if rule.get("enabled", True) else 0,
                    immutable=1 if rule.get("immutable") else 0,
                    conditions_json=json.dumps(
                        rule.get("conditions") or {}, ensure_ascii=False,
                    ),
                )
                report["rules"] += 1
            except Exception as exc:
                log.warning("speech_rule_seed_failed",
                            key=rule.get("key"), error=str(exc))

        if not self.store.list_lexicons():
            lexicon = _read_json(SEED_DIR / "lexicon_seed.json") or {}
            key = str(lexicon.get("key") or "global_sensitive_v1")
            try:
                self.store.upsert_lexicon_terms(
                    key,
                    lexicon.get("name") or key,
                    lexicon.get("terms") or [],
                    changed_by="bootstrap",
                )
                report["lexicons"] += 1
            except Exception as exc:
                log.warning("speech_lexicon_seed_failed", key=key, error=str(exc))

        self.store.invalidate()
        return report

    def validate(self) -> list[str]:
        issues: list[str] = []
        persona = self._active_persona()
        if persona is None:
            issues.append("没有 active persona，Web 端至少需要一个人设")
            return issues
        latest = self.store.get_latest_persona_version(persona.id)
        if latest is None:
            issues.append(f"persona {persona.id} 没有已发布版本")
        if not self.store.list_rules(enabled_only=False):
            issues.append("没有 policy_rules 数据")
        return issues
