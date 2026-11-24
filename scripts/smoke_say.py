# 发送路径冒烟：不真发，只验证 _exec('say') 能走完不抛异常
import asyncio, os, sys, json
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(root); sys.path.insert(0, root)
from loop import brain
async def main():
    for spontaneous in (False, True):
        obs, rec = await brain._exec('say', {'target': 'filehelper', 'text': '冒烟 1', 'why': '自检'},
                                     db='data/yinyue_meta.db', dry_run=True, turn_id='smoke',
                                     spontaneous=spontaneous)
        print('spontaneous=%s -> ok, obs=%s' % (spontaneous, str(obs)[:60]))
asyncio.run(main())