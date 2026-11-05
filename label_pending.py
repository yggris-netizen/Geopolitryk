# -*- coding: utf-8 -*-
"""Разметка pending_voices на ПК: показывает список, спрашивает ФИО, сохраняет."""
import sys

from db.pending_voices import get_pending, mark_resolved, get_pending_by_id
from db.voice_profiles import save_voice_profile, get_all_profiles


def show_pending():
    items = get_pending(limit=50)
    if not items:
        print("📭 Pending пуст")
        return []
    print(f"\n📋 Pending: {len(items)}")
    for i, p in enumerate(items, 1):
        print(f"  {i}. id={p['id']}, speaker={p.get('speaker_id') or '?'}, "
              f"dur={p.get('duration_sec', 0):.0f}s, "
              f"video={p.get('video_id', '')}")
    return items


def label_one(item):
    pid = item["id"]
    spk = item.get("speaker_id") or "?"
    dur = item.get("duration_sec", 0)
    print(f"\n{'=' * 60}")
    print(f"Разметка: id={pid}, speaker={spk}, dur={dur:.0f}s")
    print(f"  video: {item.get('video_url', '')}")
    print(f"{'=' * 60}")

    name = input("  ФИО спикера (Enter = пропустить, '-' = удалить): ").strip()
    if name == "-":
        mark_resolved(pid, "ignored")
        print(f"  ⏭ {spk} — удалён (ignored)")
        return False
    if not name:
        print(f"  ⏭ {spk} — пропущен")
        return False
    if len(name) < 3 or len(name) > 60:
        print(f"  ❌ ФИО: 3–60 символов. Пропущен.")
        return False

    pending = get_pending_by_id(pid)
    if not pending:
        print(f"  ❌ pending #{pid} не найден")
        return False

    save_voice_profile(
        name=name, bio="", role="guest",
        embedding=pending["embedding"],
        duration_sec=pending.get("duration_sec", 0),
        source_video_id=pending.get("video_id", ""),
    )
    mark_resolved(pid, "resolved")
    print(f"  ✅ {name} ({spk}) сохранён")
    return True


def main():
    print("=" * 60)
    print("РАЗМЕТКА PENDING_ VOICES НА ПК")
    print("=" * 60)

    profiles = get_all_profiles()
    print(f"\n📊 В voice_profiles: {len(profiles)}")
    for p in profiles:
        print(f"  • {p['name']} ({p['role']})")

    items = show_pending()
    if not items:
        return

    print("\nКоманды: Enter = пропустить, '-' = удалить (ignored), Ctrl+C = выход")
    saved = 0
    for item in items:
        try:
            if label_one(item):
                saved += 1
        except (KeyboardInterrupt, EOFError):
            print("\n⏹ Прервано")
            break

    print(f"\n{'=' * 60}")
    print(f"✅ Сохранено: {saved} из {len(items)}")
    print(f"📊 В voice_profiles: {len(get_all_profiles())}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()