# -*- coding: utf-8 -*-
"""Удаляет Лазарева (id=29) и Невядомского (id=56) через delete_profile."""
import os
from db.voice_profiles import delete_profile, get_all_profiles

TARGETS = [
    ("Александр Лазарев", 29),
    ("Денис Невядомский", 56),
]

# Показать до
profiles = get_all_profiles()
print(f"[LIST] voice_profiles (до): {len(profiles)}")
for p in profiles:
    if p["id"] in (29, 56):
        print(f"   id={p['id']}, name={p['name']}")

# Удалить
deleted = 0
for name, pid in TARGETS:
    # Проверка: есть ли
    found = [p for p in profiles if p["id"] == pid]
    if not found:
        print(f"[SKIP] id={pid} ({name}) — не найден")
        continue
    if found[0]["name"] != name:
        print(f"[WARN] id={pid} — имя '{found[0]['name']}' (ожидалось '{name}')")
    ok = delete_profile(pid)
    if ok:
        print(f"[OK] Удалён id={pid} ({found[0]['name']})")
        deleted += 1
    else:
        print(f"[ERR] Не удалось удалить id={pid}")

# Показать после
profiles = get_all_profiles()
print(f"\n[STAT] Удалено: {deleted}")
print(f"[STAT] voice_profiles (после): {len(profiles)}")
print(f"[STAT] Лазарев: {[p['name'] for p in profiles if 'Лазарев' in p['name']]}")
print(f"[STAT] Невядомский: {[p['name'] for p in profiles if 'Невядомский' in p['name']]}")
