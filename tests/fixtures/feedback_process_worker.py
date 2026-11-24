"""Isolated subprocess fixture: real consumer/SQLite, scripted model transport."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from core.quality import corrections
from core.terminal import event_runtime
from core.terminal.event_store_db import TerminalEventStore
from db.repo_memory_projections import MemoryProjectionRepository
from db.repo_feedback import FeedbackRepository


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("worker")
    parser.add_argument("ready")
    parser.add_argument("gate")
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()
    engine = create_engine("sqlite:///" + args.db)
    store = TerminalEventStore(engine)
    event_runtime.default_event_store = lambda: store
    corrections._owner_ids = lambda platform: {"person-a"}
    class Model:
        async def chat(self, **kwargs):
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO test_model_calls(worker) VALUES(:worker)"), {"worker": args.worker})
            Path(args.ready).touch()
            if args.hold:
                async with asyncio.timeout(30):
                    while not Path(args.gate).exists():
                        await asyncio.sleep(0.01)
            return json.dumps({"action": "upsert", "fact": "用户明确更正后的资料", "confidence": 1})
        @staticmethod
        def parse_json(value, default=None):
            return json.loads(value)
    try:
        result = await corrections.process_pending_corrections(
            meta_repo=SimpleNamespace(memory_projections=MemoryProjectionRepository(engine), feedback_samples=FeedbackRepository(engine)),
            llm=Model(), lease=True)
        print(json.dumps(result), flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
