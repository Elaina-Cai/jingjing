"""A pat must be attributed to the person who sent it, everywhere.

Why this file exists
--------------------
WeChat pats ("拍一拍") are the one message type with **no wxid anywhere**.  The
wire format is prose::

    "P5" 拍了拍 "鲸鲸"      # in a group
    "Elaina" 拍了拍我          # in a private chat
    我拍了拍 "P6"            # sent by us

``db.parser.extract_group_sender`` only understands a ``"wxid_xxx:\\n"``
prefix, so a pat returned no sender at all, ``sender_wxid`` stayed empty, and
the row was stored as ``actor_person_id='unknown'`` -- 42 of 60 pat rows in the
live database.  Everything downstream then saw one anonymous patter.

Two consequences the owner noticed:

* she mis-counted how often *each* person had patted her, because every pat
  collapsed onto the same nameless identity;
* the "what did this sender last say" helper matched a pat line from *someone
  else* and handed the model that text as the current speaker's own words.

The author is not missing, though -- it is in the prose.  ``extract_pat_sender``
reads it back out, and this file pins down both the parse and the places that
consume it.

Run directly: python tests/test_pat_author_attribution.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ------------------------------------------------------------------ the parser

def test_group_pat_yields_the_author() -> None:
    from db.parser import extract_pat_sender

    assert extract_pat_sender('"P5" 拍了拍 "鲸鲸"') == "P5"
    assert extract_pat_sender('"P4" 拍了拍 "鲸鲸"') == "P4"


def test_private_pat_yields_the_author() -> None:
    from db.parser import extract_pat_sender

    assert extract_pat_sender('"Elaina" 拍了拍我') == "Elaina"


def test_own_pat_has_no_author() -> None:
    """When *we* pat someone the parser must not return a name."""
    from db.parser import extract_pat_sender

    assert extract_pat_sender('我拍了拍 "P6"') == ""
    assert extract_pat_sender('自己拍了拍 "P6"') == ""


def test_pat_wrapped_in_a_title_tag() -> None:
    """Some clients deliver the pat as a ``<title>`` fragment."""
    from db.parser import extract_pat_sender

    assert extract_pat_sender('<msg><title>"P5" 拍了拍 "鲸鲸"</title></msg>') == "P5"


def test_non_pat_is_not_mistaken_for_one() -> None:
    from db.parser import extract_pat_sender

    assert extract_pat_sender("今天天气不错") == ""
    assert extract_pat_sender("@鲸鲸 你看这个") == ""
    assert extract_pat_sender("") == ""
    assert extract_pat_sender(None) == ""


def test_malformed_pat_fails_closed() -> None:
    """Junk must yield no name rather than a wrong one."""
    from db.parser import extract_pat_sender

    # No target, no name -- nothing safe to attribute.
    assert extract_pat_sender("拍了拍") == ""
    assert extract_pat_sender('"" 拍了拍 "鲸鲸"') == ""


def test_is_pat_title_covers_owned_pats() -> None:
    """``is_pat_title`` is true even when there is no author to extract.

    This is the whole reason it exists next to ``extract_pat_sender``: a pat we
    sent is still a pat.
    """
    from db.parser import is_pat_title

    assert is_pat_title('我拍了拍 "P6"') is True
    assert is_pat_title('"P5" 拍了拍 "鲸鲸"') is True
    assert is_pat_title('"Elaina" 拍了拍我') is True
    assert is_pat_title("今天天气不错") is False
    assert is_pat_title("") is False
    assert is_pat_title(None) is False


# -------------------------------------------------------- the transcript label

def test_transcript_names_the_pat_author() -> None:
    """``get_recent_text`` shows the author, not an unattributable ``unknown``."""
    from legacy_brain.group_state import BufferedMessage, GroupState

    state = GroupState(chat_id="g1")
    state.recent.append(
        BufferedMessage(
            sender_id="unknown",
            sender_name="unknown",
            content='"P5" 拍了拍 "鲸鲸"',
            created_at=datetime.now(),
        )
    )
    text = state.get_recent_text()
    assert "P5:" in text, text
    assert "unknown:" not in text, text


def test_transcript_keeps_a_real_sender_name() -> None:
    """A stored sender name still wins for ordinary messages."""
    from legacy_brain.group_state import BufferedMessage, GroupState

    state = GroupState(chat_id="g1")
    state.recent.append(
        BufferedMessage(
            sender_id="wxid_seed01",
            sender_name="蔡神",
            content="你在干啥",
            created_at=datetime.now(),
        )
    )
    assert "蔡神: 你在干啥" in state.get_recent_text()


def test_transcript_labelling_survives_a_bad_message() -> None:
    """A message that cannot be parsed is labelled, not crashed on."""
    from legacy_brain.group_state import GroupState

    state = GroupState(chat_id="g1")
    state.recent.append(SimpleNamespace(sender_name=None, content=None, created_at=None))
    text = state.get_recent_text()
    assert "unknown" in text


def test_own_pat_keeps_the_stored_name() -> None:
    """When we pat someone there is no author to lift, so keep the real name."""
    from legacy_brain.group_state import BufferedMessage, GroupState

    state = GroupState(chat_id="g1")
    state.recent.append(
        BufferedMessage(
            sender_id="wxid_me",
            sender_name="鲸鲸",
            content='我拍了拍 "P6"',
            created_at=datetime.now(),
        )
    )
    assert "鲸鲸: " in state.get_recent_text()


def main() -> bool:
    tests = [
        test_group_pat_yields_the_author,
        test_private_pat_yields_the_author,
        test_own_pat_has_no_author,
        test_pat_wrapped_in_a_title_tag,
        test_non_pat_is_not_mistaken_for_one,
        test_malformed_pat_fails_closed,
        test_is_pat_title_covers_owned_pats,
        test_transcript_names_the_pat_author,
        test_transcript_keeps_a_real_sender_name,
        test_transcript_labelling_survives_a_bad_message,
        test_own_pat_keeps_the_stored_name,
    ]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print("\nall passed" if not failed else f"\nfailed: {', '.join(failed)}")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
