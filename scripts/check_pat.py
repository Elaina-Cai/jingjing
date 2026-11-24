import os
import sqlite3
conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'yinyue.db'))
rows = conn.execute(
    'SELECT msg_id, msg_type, msg_type_name, msg_sub_type, content, sender_id, sender_name, chat_id '
    'FROM messages ORDER BY synced_at DESC LIMIT 5'
).fetchall()
for r in rows:
    c = (r[4] or "")[:120]
    print(f'id={r[0]}, type={r[1]}, type_name={r[2]!r}, sub={r[3]}, sender={r[5]!r}/{r[6]!r}, chat={r[7]!r}')
    print(f'  content={c!r}')
conn.close()
