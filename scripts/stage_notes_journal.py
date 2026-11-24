"""Stage an append-only note journal from an unchanged JSONL snapshot."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def stage(source: Path, destination: Path) -> tuple[int, str]:
    raw = source.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("note_snapshot_contains_non_object")
    digest = hashlib.sha256(raw).hexdigest()
    with destination.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(destination)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE note_revisions (seq INTEGER PRIMARY KEY, "
                "note_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute("CREATE INDEX note_revision_lookup ON note_revisions(note_id, revision)")
            connection.execute("CREATE TABLE note_snapshot (source_hash TEXT NOT NULL, row_count INTEGER NOT NULL)")
            connection.executemany(
                "INSERT INTO note_revisions(note_id, revision, payload) VALUES(?,?,?)",
                [(str(row.get("note_id") or ""), max(1, int(row.get("revision") or 1)),
                  json.dumps(row, ensure_ascii=False)) for row in rows],
            )
            replay = [json.loads(payload) for (payload,) in connection.execute(
                "SELECT payload FROM note_revisions ORDER BY seq"
            )]
            if replay != rows or source.read_bytes() != raw:
                raise ValueError("note_snapshot_changed")
            connection.execute("INSERT INTO note_snapshot VALUES(?,?)", (digest, len(rows)))
    except BaseException:
        destination.unlink()
        raise
    return len(rows), digest


def export_rollback(journal: Path, original: Path, destination: Path) -> int:
    """Prepare a rollback file with all journal writes after the original snapshot."""
    raw = original.read_bytes()
    with closing(sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        snapshots = connection.execute("SELECT source_hash, row_count FROM note_snapshot").fetchall()
        if len(snapshots) != 1 or snapshots[0][0] != hashlib.sha256(raw).hexdigest():
            raise ValueError("original_notes_changed_since_snapshot")
        rows = connection.execute("SELECT payload FROM note_revisions ORDER BY seq").fetchall()
    original_rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if len(original_rows) != snapshots[0][1] or [json.loads(row[0]) for row in rows[:len(original_rows)]] != original_rows:
        raise ValueError("journal_prefix_mismatch")
    tail = rows[len(original_rows):]
    with destination.open("xb") as stream:
        stream.write(raw)
        if raw and not raw.endswith(b"\n"):
            stream.write(b"\n")
        for (payload,) in tail:
            stream.write(payload.encode("utf-8") + b"\n")
    return len(tail)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--rollback-from", type=Path, metavar="JOURNAL")
    args = parser.parse_args()
    staging = Path(__file__).resolve().parents[1] / ".test_tmp"
    if not args.destination.resolve().is_relative_to(staging):
        parser.error("destination must be in .test_tmp")
    if args.rollback_from is not None:
        print(f"Exported {export_rollback(args.rollback_from, args.source, args.destination)} post-cutover revisions")
        return
    count, digest = stage(args.source, args.destination)
    print(f"Staged {count} revisions, source SHA-256 {digest}")


if __name__ == "__main__":
    main()
