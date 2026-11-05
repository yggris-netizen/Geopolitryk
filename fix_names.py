# -*- coding: utf-8 -*-
"""Исправляет битые имена в voice_profiles после PowerShell-разметки.

Удаляет все профили кроме Шелеста (id=1), затем пересоздаёт из pending
с правильными именами (кириллица в UTF-8 файле, не в PowerShell).
"""
import sqlite3

from config import DB_PATH
from db.pending_voices import get_pending_by_id, mark_resolved
from db.voice_profiles import save_voice_profile, get_all_profiles

# Правильные имена для pending_id (из прошлой разметки)
CORRECT = {
    18: "Виталий Дикий",
    16: "Игорь Мосейчук",
    13: "Помощница",
    11: "Дмитрий Спивак",
    12: "Эдуард Глёза",
}

# id профиля Шелеста — оставляем
KEEP_PROFILE_ID = 1


def main():
    print("=" * 60)
    print("ИСПРАВЛЕНИЕ ИМЁН В VOICE_PROFILES")
    print("=" * 60)

    # 1. Показываем текущие профили
    print("\n📊 Текущие профили:")
    for p in get_all_profiles():
        print(f"  id={p['id']}, name={p['name']!r}, role={p['role']}")

    # 2. Удаляем все профили кроме KEEP_PROFILE_ID
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        deleted = conn.execute(
            "DELETE FROM voice_profiles WHERE id != ?", (KEEP_PROFILE_ID,)
        ).rowcount
        conn.commit()
        print(f"\n🧹 Удалено профилей (кроме id={KEEP_PROFILE_ID}): {deleted}")
    finally:
        conn.close()

    # 3. Пересоздаём из pending с правильными именами
    print(f"\n🔧 Пересоздаём из pending ({len(CORRECT)} шт.):")
    saved = 0
    for pid, name in CORRECT.items():
        pending = get_pending_by_id(pid)
        if not pending:
            print(f"  ⚠️  pending #{pid} не найден — пропускаю")
            continue
        save_voice_profile(
            name=name, bio="", role="guest",
            embedding=pending["embedding"],
            duration_sec=pending.get("duration_sec", 0),
            source_video_id=pending.get("video_id", ""),
        )
        mark_resolved(pid, "resolved")
        print(f"  ✅ #{pid} → {name}")
        saved += 1

    # 4. Итог
    print(f"\n{'=' * 60}")
    print(f"✅ Сохранено: {saved} из {len(CORRECT)}")
    print(f"\n📊 Итоговые профили:")
    for p in get_all_profiles():
        print(f"  id={p['id']}, name={p['name']!r}, role={p['role']}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()