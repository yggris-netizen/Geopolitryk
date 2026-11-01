"""Одноразовая чистка дублей в experts_seen_today.
Удаляет записи с тем же набором слов, но другим порядком
(«Воскресенский Юрий» vs «Юрий Воскресенский»).
Оставляет ПОСЛЕДНЮЮ по времени (новый формат «Имя Фамилия»).
"""

import sqlite3
from config import DB_PATH


def _norm_key(name: str) -> frozenset:
    return frozenset(w.lower() for w in name.split() if len(w) > 1)


conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT date, expert_name, seen_at FROM experts_seen_today ORDER BY seen_at ASC"
).fetchall()

groups = {}
for r in rows:
    key = (r["date"], _norm_key(r["expert_name"]))
    groups.setdefault(key, []).append((r["expert_name"], r["seen_at"]))

to_delete = []
for (date, words), items in groups.items():
    if len(items) > 1:
        keep_name, _ = items[-1]
        for n, _ in items[:-1]:
            if n != keep_name:
                to_delete.append((date, n))

print(f"Дублей найдено: {len(to_delete)}")
for d, n in to_delete:
    print(f"  delete: {d} | {n}")

if to_delete:
    for d, n in to_delete:
        conn.execute(
            "DELETE FROM experts_seen_today WHERE date = ? AND expert_name = ?",
            (d, n)
        )
    conn.commit()
    print(f"✅ Удалено: {len(to_delete)}")
else:
    print("Дублей нет — чисто.")

conn.close()