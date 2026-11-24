"""为显式绑定的隔离旧库安装投影停写闸；默认 dry，--apply 才写入。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from core.memory.legacy_read_binding import load_read_binding
from core.memory.legacy_write_freeze import freeze_projection_writes


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv)
    try:
        result=freeze_projection_writes(load_read_binding(args.binding),dry_run=not args.apply)
    except (OSError,ValueError,sqlite3.Error) as exc:
        print(json.dumps({'error':str(exc),'frozen':None},ensure_ascii=False))
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
