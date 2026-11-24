#!/usr/bin/env python
"""读取/校验群聊理解回放场景集（批0 产出，只读）。

场景集文件：configs/understanding_scenarios.json
本工具只做加载、校验与展示；真正的回放（把场景喂进当前链路）在批 1 接。

用法：
  python scripts/understanding_scenarios.py --list
  python scripts/understanding_scenarios.py --show S2_unknown_slang
  python scripts/understanding_scenarios.py --check        # 字段完整性校验
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "configs" / "understanding_scenarios.json"

REQUIRED_SCENARIO_FIELDS = ("id", "title", "chat", "messages", "trigger", "expect", "check")
REQUIRED_EXPECT_FIELDS = ("behavior", "must", "must_not")


def load(path: Path = DEFAULT_PATH) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError(f"no scenarios in {path}")
    return scenarios


def validate(scenarios: list[dict]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for index, scenario in enumerate(scenarios):
        where = f"#{index + 1}"
        if not isinstance(scenario, dict):
            problems.append(f"{where}: not an object")
            continue
        sid = scenario.get("id") or where
        if sid in seen:
            problems.append(f"{sid}: duplicate id")
        seen.add(sid)
        for field in REQUIRED_SCENARIO_FIELDS:
            if field not in scenario:
                problems.append(f"{sid}: missing '{field}'")
        expect = scenario.get("expect") or {}
        for field in REQUIRED_EXPECT_FIELDS:
            if field not in expect:
                problems.append(f"{sid}: expect missing '{field}'")
        for field in ("messages", "must", "must_not", "behavior"):
            value = scenario.get(field, expect.get(field))
            if isinstance(value, list) and not value and field != "must":
                problems.append(f"{sid}: '{field}' is empty")
    return problems


def show(scenario: dict) -> None:
    print(f"[{scenario['id']}] {scenario['title']}")
    chat = scenario.get("chat") or {}
    print(f"  会话: {chat.get('type', '?')} / {chat.get('name', '?')}")
    print("  消息:")
    for message in scenario.get("messages", []):
        print(f"    {message.get('speaker', '?')}: {message.get('content', '')}")
    trigger = scenario.get("trigger") or {}
    print(f"  触发: {trigger.get('speaker', '?')}: {trigger.get('content', '')}")
    expect = scenario.get("expect") or {}
    print(f"  期望行为: {', '.join(expect.get('behavior', []))}")
    for label, key in (("必须", "must"), ("禁止", "must_not")):
        values = expect.get(key) or []
        if values:
            print(f"  {label}:")
            for value in values:
                print(f"    - {value}")
    print(f"  检查: {scenario.get('check', '')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="群聊理解场景集（只读）")
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--list", action="store_true", help="列出全部场景")
    parser.add_argument("--show", help="展示某个场景（按 id）")
    parser.add_argument("--check", action="store_true", help="校验字段完整性")
    args = parser.parse_args()

    path = Path(args.path)
    scenarios = load(path)

    if args.check:
        problems = validate(scenarios)
        if problems:
            print("[check] problems:")
            for problem in problems:
                print("  -", problem)
            return 1
        print(f"[check] OK: {len(scenarios)} scenarios, fields complete")
        return 0

    if args.show:
        for scenario in scenarios:
            if scenario.get("id") == args.show:
                show(scenario)
                return 0
        print(f"[err] scenario not found: {args.show}")
        return 2

    # 默认 --list
    print(f"{len(scenarios)} scenarios in {path}")
    for scenario in scenarios:
        expect = scenario.get("expect") or {}
        behaviors = ",".join(expect.get("behavior", []))
        print(f"  {scenario.get('id', '?'):<22} {scenario.get('title', ''):<28} → {behaviors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
