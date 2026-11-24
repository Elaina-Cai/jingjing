"""交付物渲染（``plan.Artifact`` 那一槽）。见 ``render.py`` 模块 docstring。"""

from core.artifacts.render import (  # noqa: F401
    ACCOMPANY_MAX_CHARS,
    ARTIFACT_DIR,
    ArtifactError,
    KINDS,
    MAX_CARD_CHARS,
    MAX_PAGE_CHARS,
    accompany_text,
    artifact_of,
    check_kind,
    describe,
    render_card_png,
    slugify,
    to_deliver,
    write_card,
    write_page,
)
