# -*- coding: utf-8 -*-
import sqlite3
from config import DB_PATH

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

print("=== Схема transcripts_cache ===")
for r in conn.execute("PRAGMA table_info(transcripts_cache)"):
    print(f"  {r['name']} ({r['type']})")

print("\n=== Схема predictions ===")
for r in conn.execute("PRAGMA table_info(predictions)"):
    print(f"  {r['name']} ({r['type']})")

print("\n=== Пример записи из transcripts_cache (первые 500 символов) ===")
row = conn.execute("SELECT * FROM transcripts_cache LIMIT 1").fetchone()
if row:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, str) and len(v) > 500:
            print(f"  {k}: {v[:500]!r}... (len={len(v)})")
        else:
            print(f"  {k}: {v!r}")
else:
    print("  (пусто)")

print("\n=== Пример записи из predictions (1 шт.) ===")
row = conn.execute("SELECT * FROM predictions LIMIT 1").fetchone()
if row:
    for k, v in dict(row).items():
        print(f"  {k}: {v!r}")
else:
    print("  (пусто)")

conn.close()