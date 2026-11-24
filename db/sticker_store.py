"""Per-account sticker registry, and the cross-account view over it.

Storage follows docs/data_isolation_architecture.md:

    data/platforms/<platform>/<account_id>/emoji/
        registry.json        emotion -> how to find it on screen
        templates/<id>.png   the source image used by the matcher
        thumbs/<id>.png      optional crop learned from the popup itself

Why per-account is not optional
-------------------------------

The isolation document is explicit that each account's custom stickers are
entirely its own ("主人号的自定义表情，和鲸鲸号完全不同").  The same emotion word
therefore resolves to a different picture depending on whose account is
sending.  Any design that keeps one global sticker list is wrong by
construction, not merely untidy.

Reading across accounts
-----------------------

The super terminal has to be able to see every platform and every account.
The routing rules here mirror UnifiedQuery exactly, so there is one vocabulary
for "whose data is this":

    name an account   -> that account only
    name a platform   -> every account on that platform, merged
    name neither      -> every platform, every account, merged

Each returned record carries platform + account_id, so a caller can always tell
where a sticker came from.

Naming note
-----------

docs/data_isolation_architecture.md calls the read API get_emoji(); this module
uses sticker vocabulary (StickerStore.get / list_stickers) because "emoji" in
this codebase already means something else -- the decrypted WeChat emoji cache
in data/emoji_cache.  The on-disk directory is emoji/ as the document specifies.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Optional

import structlog
from safelog import warn

log = structlog.get_logger()

__all__ = [
    "Sticker", "StickerStore", "store", "list_stores", "all_stickers",
    "sendable_stickers", "find_sticker", "manifest", "tag_index",
    "owner_description_index",
    "default_tab_index", "migrate_legacy", "migrate_all",
    "StickerCandidate", "match_candidates", "looks_encoded",
]

# The popup's tab strip reads [search, emoji, custom]; custom is the rightmost.
default_tab_index = 2


def _coerce_tab(value) -> int:
    """Normalise legacy tab values ("自定义", missing, ...) to an index."""
    # 2026-11-01：老 registry 里没有 tab 键，值是 None —— int(None) 每次读都抛
    # TypeError 再被吞成 warn，一小时 71 条，把体检的"静默告警"指标全占了。
    # "没有这个字段"是正常情况，不是异常：直接回默认值，不留日志。
    if value is None or isinstance(value, bool):
        return default_tab_index
    try:
        return int(value)
    except (TypeError, ValueError):
        warn("db/sticker_store.py:74 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return default_tab_index


# Two live entries (点头 / 敬礼，遵命) carry a 44-char base64 blob in ``label``
# from an import accident, and that blob was being rendered as the sticker's
# description -- in the model prompt and in the manager UI.  A label shaped like
# that is machine noise, not wording.  The registry keeps it untouched (the raw
# record is the substrate); every read-time projection skips it.
_ENCODED_LABEL_RE = re.compile(r"^[A-Za-z0-9+/=]{24,}$")


def looks_encoded(text: str) -> bool:
    """Whether a field is a base64-shaped blob rather than human wording.

    Two shapes count: the alphabet plus a ``+ / =`` character (the accident that
    was really there was ``Cg4KB2RlZmF1bHQSA+WvuQoOCgdkZWZhdWx0EgPlr7k=``), or a
    40+ character run of letters and digits with no space.  A short word, a
    Chinese phrase, or a slug with an underscore is never flagged.
    """
    value = (text or "").strip()
    if len(value) < 24 or " " in value:
        return False
    if not _ENCODED_LABEL_RE.match(value):
        return False
    return any(ch in value for ch in "+/=") or len(value) >= 40


def _paths():
    """The single source of path truth; see core/config/settings.py."""
    from core.config.settings import config

    return config.paths


@dataclass(frozen=True)
class Sticker:
    """One emotion on one account."""

    emotion: str
    platform: str
    account_id: str
    sticker_id: str = ""
    label: str = ""
    aliases: tuple = ()
    context: str = ""
    scope: tuple = ()
    tab: int = default_tab_index
    template: str = ""
    thumb: str = ""

    @property
    def templates(self) -> tuple:
        """All source images for this emotion, in preference order."""
        if isinstance(self.template, (list, tuple)):
            return tuple(t for t in self.template if t)
        return (self.template,) if self.template else ()

    @property
    def description(self) -> str:
        """The human wording for this sticker: a read-time lens, not a rewrite.

        ``context`` is the owner's own gloss, ``label`` the coarse tag; a
        machine-noise label is skipped instead of rendered.  Nothing is removed
        from the record, so a better lens can still be written later.
        """
        for value in (self.context, self.label):
            text = (value or "").strip()
            if text and not looks_encoded(text):
                return text
        return self.emotion

    def images(self, root: Path | None = None) -> list:
        """Images to try when matching, best first.

        A learned thumbnail is a pixel-exact copy of what WeChat paints, so it
        matches far more reliably than a full-resolution source image and is
        always tried first.

        Two path shapes exist and both have to resolve.  ``save()`` and the
        account migration write a name relative to the sticker root
        (``templates/<id>.png``), while two web endpoints wrote the full path
        instead (``data\\platforms\\...\\emoji\\templates\\<id>.png``).
        Prefixing the root unconditionally turned the second shape into
        ``<root>/data/platforms/...``, which can never exist: four registered
        emotions resolved to zero images and every send of them died with
        ``sticker_has_no_images``.  So try the path as given -- the working
        directory is the project root everywhere this runs -- and fall back to
        the root-relative reading.
        """
        out = []
        for candidate in (self.thumb,) + self.templates:
            if not candidate:
                continue
            path = Path(candidate)
            if not path.is_absolute() and not path.exists() and root is not None:
                path = root / path
            if path.exists() and str(path) not in out:
                out.append(str(path))
        return out

    def with_fields(self, **changes) -> "Sticker":
        return replace(self, **changes)

    def to_registry(self) -> dict:
        """The on-disk form. Kept close to the shape the design document shows."""
        entry = {
            "id": self.sticker_id,
            "tab": self.tab,
            "template": self.template,
        }
        for name in ("label", "context", "thumb"):
            value = getattr(self, name)
            if value:
                entry[name] = value
        for name in ("aliases", "scope"):
            values = list(getattr(self, name) or ())
            if values:
                entry[name] = values
        return entry


@dataclass(frozen=True)
class StickerCandidate:
    """One entry that could be what the model meant, and why it was picked up.

    ``siblings`` names the other entries the *same* alias also matched, so a
    caller sees the collision up front instead of after a wrong send.
    """

    sticker: Sticker
    matched_by: str          # "name" | "alias"
    matched_field: str       # which registry field the hit came from
    matched_value: str       # the model's own words that matched
    siblings: tuple = ()

    @property
    def emotion(self) -> str:
        return self.sticker.emotion

    def as_dict(self) -> dict:
        return {
            "emotion": self.sticker.emotion,
            "matched_by": self.matched_by,
            "field": self.matched_field,
            "value": self.matched_value,
            "siblings": list(self.siblings),
            "description": self.sticker.description,
            "scope": list(self.sticker.scope or ()),
        }


def match_candidates(key: str, stickers: Iterable[Sticker]) -> tuple:
    """Every entry ``key`` could mean, each carrying the reason it matched.

    One implementation, shared by the tool (catalog across accounts) and the
    sender (one account's registry), so the two halves cannot drift.

    An exact name is not ambiguous and short-circuits -- that is ``resolve``''s
    contract, kept.  Aliases are a different matter: measured on the live
    registry, five alias groups are claimed by two entries each (``你好`` ->
    你好 / 嗯; ``发呆`` -> 你说我吗？(呆呆的，装傻) / 喝奶茶，看戏; ``愤怒`` ->
    太菜了 / 我已急哭; ``对的`` -> 点头 / 敬礼，遵命; ``嘿嘿`` -> 我不伺候了 /
    你看你又急了).  Returning the first hit silently is how the wrong picture
    shipped, so every hit is returned and the caller has to face the choice.
    """
    name = (key or "").strip()
    if not name:
        return ()
    entries = [s for s in stickers if s is not None]
    for sticker in entries:
        if sticker.emotion == name:
            return (StickerCandidate(sticker, "name", "emotion", name),)
    hits = [s for s in entries if name in tuple(s.aliases or ())]
    if not hits:
        return ()
    names = tuple(s.emotion for s in hits)
    return tuple(
        StickerCandidate(sticker, "alias", "aliases", name,
                         tuple(n for n in names if n != sticker.emotion))
        for sticker in hits
    )


class StickerStore:
    """One account's sticker registry. Cheap to construct, reads on demand."""

    def __init__(self, platform: str, account_id: str = "", *, paths=None):
        self.platform = platform
        self.account_id = account_id
        self._paths = paths

    @property
    def paths(self):
        if self._paths is None:
            self._paths = _paths()
        return self._paths

    @property
    def root(self) -> Path:
        return Path(self.paths.get_account_emoji_dir(self.platform, self.account_id))

    @property
    def registry_path(self) -> Path:
        return self.root / "registry.json"

    @property
    def templates_dir(self) -> Path:
        return self.root / "templates"

    @property
    def thumbs_dir(self) -> Path:
        return self.root / "thumbs"

    def exists(self) -> bool:
        return self.registry_path.exists()

    # -- reading -----------------------------------------------------------------

    def load(self) -> dict:
        """Every sticker on this account, keyed by emotion."""
        if not self.registry_path.exists():
            return {}
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("sticker_registry_unreadable",
                        path=str(self.registry_path), error=str(exc))
            return {}

        out = {}
        for emotion, entry in (raw or {}).items():
            if not isinstance(entry, dict):
                continue
            out[emotion] = Sticker(
                emotion=emotion,
                platform=self.platform,
                account_id=self.account_id,
                sticker_id=str(entry.get("id") or ""),
                label=str(entry.get("label") or emotion),
                aliases=tuple(entry.get("aliases") or ()),
                context=str(entry.get("context") or ""),
                scope=tuple(entry.get("scope") or ()),
                tab=_coerce_tab(entry.get("tab")),
                template=entry.get("template") or "",
                thumb=str(entry.get("thumb") or ""),
            )
        return out

    def get(self, emotion: str) -> Optional[Sticker]:
        return self.load().get(emotion)

    def resolve(self, key: str) -> Optional[Sticker]:
        """Find a sticker by exact name, then by a declared alias.

        Resolution order: an exact name wins, then a declared alias.
        The old character-overlap guess is gone -- it silently mis-sent
        stickers (the model asked for one, the fuzzy match shipped
        another).  Aliases remain only so legacy entries keep resolving
        until they are renamed; new entries are matched by name alone.
        """
        if not key:
            return None
        stickers = self.load()
        if key in stickers:
            return stickers[key]

        for sticker in stickers.values():
            if key in sticker.aliases:
                log.info("sticker_matched_by_alias", input=key, resolved=sticker.emotion,
                         account_id=self.account_id)
                return sticker
        return None

    def candidates(self, key: str) -> tuple:
        """The read-time lens over this registry: every hit, with its reason.

        ``resolve`` still answers "the one entry this name means" for callers
        that need a single value; this answers "what could these words mean",
        which is the question a free-text request actually asks.
        """
        return match_candidates(key, self.load().values())

    def encoded_labels(self) -> tuple:
        """Emotions whose ``label`` is machine noise.  The gauge for this fix."""
        return tuple(s.emotion for s in self.load().values()
                     if looks_encoded(s.label))

    def rename_emotion(self, old: str, new: str) -> Sticker:
        """Rename a sticker: the name is the id, so this is a key change.

        The template file keeps its own filename; only the registry key
        and the sticker_id move.
        """
        new = (new or "").strip()
        if not new:
            raise ValueError("新名称不能为空")
        stickers = self.load()
        sticker = stickers.pop(old, None)
        if sticker is None:
            raise ValueError(f"表情 '{old}' 不存在")
        if new in stickers:
            stickers[old] = sticker
            raise ValueError(f"名称 '{new}' 已被使用")
        renamed = sticker.with_fields(emotion=new, sticker_id=new)
        stickers[new] = renamed
        self.save(stickers.values())
        return renamed

    # -- writing ----------------------------------------------------------------

    def template_path(self, sticker_id: str, suffix: str = ".png") -> Path:
        return self.templates_dir / (sticker_id + suffix)

    def save(self, stickers: Iterable[Sticker]) -> Path:
        payload = {}
        for sticker in stickers:
            entry = sticker.to_registry()
            if not entry.get("id"):
                entry["id"] = sticker.emotion
            payload[sticker.emotion] = entry

        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.registry_path)
        log.info("sticker_registry_saved", path=str(self.registry_path),
                 count=len(payload), account_id=self.account_id)
        return self.registry_path

    def upsert(self, emotion: str, **fields) -> Sticker:
        stickers = self.load()
        current = stickers.get(emotion)
        if current is None:
            current = Sticker(emotion=emotion, platform=self.platform,
                              account_id=self.account_id, sticker_id=emotion)
        updated = current.with_fields(**fields)
        stickers[emotion] = updated
        self.save(stickers.values())
        return updated

    def delete(self, emotion: str, *, remove_files: bool = True) -> bool:
        stickers = self.load()
        sticker = stickers.pop(emotion, None)
        if sticker is None:
            return False
        self.save(stickers.values())
        if remove_files:
            for image in sticker.images(self.root):
                try:
                    Path(image).unlink()
                except Exception:
                    warn("db/sticker_store.py:425 Exception", err=repr(__import__("sys").exc_info()[1]))
        return True


# --------------------------------------------------------------------------------------
# One-image-one-name migration
# --------------------------------------------------------------------------------------

def migrate_legacy(store: StickerStore, *, dry_run: bool = True) -> dict:
    """Migrate one account's registry to one-image-one-name.

    What it does, per entry:

    - Wash every template path to the canonical root-relative
      ``templates/<file>`` shape.  The collect endpoint used to append
      ``data\\platforms\\...`` paths to existing moods, which resolve
      only while the process runs from the project root -- they die the
      day the checkout moves.
    - Split a multi-image emotion into one entry per image.  The first
      image keeps the name; the rest get ``名字2``, ``名字3``, ... as
      placeholders for you to rename by hand.
    - aliases/context are kept on the data (old readers still want
      them); the new selection path simply stops consuming them.

    With ``dry_run=True`` nothing is written and the plan is returned;
    otherwise ``registry.json`` is backed up next to itself before the
    rewrite (``registry.json.bak-<timestamp>``).
    """
    stickers = store.load()
    templates_dir = store.templates_dir

    def _wash(template_value: str) -> tuple[str, bool]:
        name = Path(template_value).name
        canonical = "templates/" + name
        if (templates_dir / name).exists():
            return canonical, True
        # Not in templates/ -- try the shape as given, then root-relative.
        for candidate in (Path(template_value), store.root / template_value):
            if candidate.exists():
                return canonical, True
        return template_value, False

    out: dict = {}
    washed_count = 0
    missing: list = []

    for emotion, sticker in sorted(stickers.items()):
        templates = [t for t in sticker.templates] or ([sticker.template] if sticker.template else [])
        if not templates:
            out[emotion] = sticker
            continue

        images: list = []
        for t in templates:
            canonical, exists = _wash(t)
            if not exists:
                missing.append(t)
                continue
            if canonical != t:
                washed_count += 1
            images.append(canonical)

        if not images:
            # Nothing resolvable; keep the entry untouched so it stays visible
            # to whoever curates the library instead of vanishing silently.
            out[emotion] = sticker
            continue

        for i, image in enumerate(images):
            name = emotion if i == 0 else f"{emotion}{i + 1}"
            while name in out:
                suffix = 2
                candidate = f"{name}{suffix}"
                while candidate in out:
                    suffix += 1
                    candidate = f"{name}{suffix}"
                name = candidate
            out[name] = sticker.with_fields(emotion=name, sticker_id=name,
                                            template=image)

    # Record which new names each old multi-image emotion produced.
    split_report: dict = {}
    for emotion, sticker in stickers.items():
        if len([t for t in sticker.templates]) > 1:
            split_report[emotion] = [k for k in out.keys()
                                     if k == emotion or k.startswith(emotion)]

    plan = {
        "account": store.account_id or store.platform,
        "before": len(stickers),
        "after": len(out),
        "washed_paths": washed_count,
        "missing_images": missing,
        "splits": split_report,
        "names": sorted(out.keys()),
    }
    if dry_run:
        plan["dry_run"] = True
        return plan

    if out != stickers:
        backup = store.registry_path.with_suffix(
            ".json.bak-" + time.strftime("%Y%m%d-%H%M%S"))
        if store.registry_path.exists():
            shutil.copy2(store.registry_path, backup)
            plan["backup"] = str(backup)
        store.save(out.values())
    return plan


def migrate_all(platform: str | None = "wechat", *, dry_run: bool = True) -> list:
    """Run the one-image-one-name migration over every account."""
    return [migrate_legacy(s, dry_run=dry_run) for s in list_stores(platform)]


# --------------------------------------------------------------------------------------
# Cross-account view
# --------------------------------------------------------------------------------------

def list_stores(platform: str | None = None,
                account_id: str | None = None) -> list:
    """Every sticker store matching the filter, most specific routing first.

    Enumeration walks the isolation layout on disk rather than the account
    database, so an account whose stickers exist but whose account.db is missing
    or stale is still visible to the super terminal.
    """
    if platform and account_id:
        return [StickerStore(platform, account_id)]

    root = Path(_paths().platforms_dir)
    if not root.exists():
        return []

    stores = []
    for platform_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if platform and platform_dir.name != platform:
            continue
        for account_dir in sorted(p for p in platform_dir.iterdir() if p.is_dir()):
            if account_id and account_dir.name != account_id:
                continue
            stores.append(StickerStore(platform_dir.name, account_dir.name))
    return stores


def store(platform: str, account_id: str = "") -> StickerStore:
    """The store for one account."""
    return StickerStore(platform, account_id)


def all_stickers(platform: str | None = None, account_id: str | None = None) -> list:
    """Every sticker on every matching account, each tagged with its origin."""
    out = []
    for candidate in list_stores(platform, account_id):
        try:
            out.extend(candidate.load().values())
        except Exception as exc:
            log.warning("sticker_store_load_failed", platform=candidate.platform,
                        account_id=candidate.account_id, error=str(exc))
    return out


def sendable_stickers(platform: str | None = None,
                      account_id: str | None = None) -> list:
    """Only the stickers that resolve to at least one real image file.

    A registry entry outlives the picture it points at: the entry still claims
    the emotion exists, but there is nothing to match on screen, so a send dies
    at resolution with ``sticker_has_no_images``.  Advertising those in the
    prompt is how the model was told "得意" was available and then failed every
    time it chose it.  Anything that describes the collection to the model has
    to promise only what the sender can deliver.
    """
    roots: dict = {}
    out = []
    for sticker in all_stickers(platform, account_id):
        key = (sticker.platform, sticker.account_id)
        if key not in roots:
            roots[key] = StickerStore(*key).root
        if sticker.images(roots[key]):
            out.append(sticker)
    return out


_TEMPLATE_MD5_RE = re.compile(r"([0-9a-f]{32})")


def owner_description_index(platform: str | None = None,
                            account_id: str | None = None) -> dict:
    """Map ``md5 -> emotion`` for every sticker the owner has named.

    The md5 lives in the template filename (``templates/<md5>.png``) and is the
    same md5 an inbound sticker carries, so it is the one key that lets the
    receiving side find the owner's own wording for a picture.

    Without this the two halves of the sticker feature never met: the send side
    knew the owner's names ("太菜了"), while the receive side described the very
    same picture with whatever the vision model guessed ("奖杯无奈叹气"), and
    the owner's wording -- which they explicitly asked to be authoritative --
    was never even loaded.  Read-only and cheap: 16 entries on the live store.
    """
    index: dict[str, str] = {}
    for sticker in all_stickers(platform, account_id):
        emotion = (sticker.emotion or "").strip()
        if not emotion:
            continue
        for candidate in (sticker.thumb,) + sticker.templates:
            if not candidate:
                continue
            match = _TEMPLATE_MD5_RE.search(Path(str(candidate)).stem)
            if match:
                # First writer wins: a later duplicate must not shadow the name
                # an earlier entry already claimed for the same picture.
                index.setdefault(match.group(1), emotion)
    return index


def _colliding_emotions(stickers) -> set:
    """Emotions that exist on more than one account, so need qualifying."""
    seen, collisions = set(), set()
    for sticker in stickers:
        if sticker.emotion in seen:
            collisions.add(sticker.emotion)
        seen.add(sticker.emotion)
    return collisions


def _image_url(sticker: "Sticker") -> str:
    templates = sticker.templates
    if not templates:
        return ""
    name = Path(templates[0]).name
    return "/api/stickers/template/%s?account=%s" % (name, sticker.account_id)


def manifest(platform: str | None = None, account_id: str | None = None) -> dict:
    """{emotion: {url, label, account_id}} -- the shape the sticker page expects.

    The web UI and the recommend_sticker tool were built against a flat manifest
    file that lived in web/static/stickers/, a second sticker system that had
    nothing to do with what could actually be sent.  They read a projection of
    the real per-account registry now.
    """
    stickers = all_stickers(platform, account_id)
    # Only disambiguate when the same emotion really exists on several
    # accounts; otherwise the plain name is what the UI should show.
    collisions = _colliding_emotions(stickers)

    out = {}
    for sticker in stickers:
        key = sticker.emotion
        if sticker.emotion in collisions:
            key = "%s@%s" % (sticker.emotion, sticker.account_id)
        out[key] = {
            "url": _image_url(sticker),
            "label": sticker.label or sticker.emotion,
            "account_id": sticker.account_id,
            "platform": sticker.platform,
        }
    return out


def tag_index(platform: str | None = None, account_id: str | None = None) -> dict:
    """{emotion: {tags, desc, url, label}} -- the shape the tag view expects.

    Aliases are the hand-curated keyword set for an emotion, so they serve
    directly as its tags; context is the sentence describing when to use it.
    """
    stickers = all_stickers(platform, account_id)
    collisions = _colliding_emotions(stickers)

    out = {}
    for sticker in stickers:
        key = sticker.emotion
        if sticker.emotion in collisions:
            key = "%s@%s" % (sticker.emotion, sticker.account_id)
        tags = list(sticker.aliases) or [sticker.emotion]
        out[key] = {
            "tags": tags,
            "desc": sticker.context or sticker.label or sticker.emotion,
            "url": _image_url(sticker),
            "label": sticker.label or sticker.emotion,
            "account_id": sticker.account_id,
        }
    return out


def find_sticker(emotion: str, platform: str | None = None,
                 account_id: str | None = None) -> Optional[Sticker]:
    """Resolve an emotion, preferring the named account then the rest.

    The named account is authoritative: if the caller said whose stickers to
    use, another account's collection must never be substituted silently.
    """
    if platform and account_id:
        return StickerStore(platform, account_id).resolve(emotion)

    for candidate in list_stores(platform, account_id):
        found = candidate.resolve(emotion)
        if found is not None:
            return found
    return None
