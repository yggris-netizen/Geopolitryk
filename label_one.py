# -*- coding: utf-8 -*-
"""Разметка одного pending_voices по id.

Использование:
    python label_one.py <pending_id> "<ФИО>"
    python label_one.py <pending_id> "-"     (удалить, ignored)

Пример:
    python label_one.py 20 "Эдуард Глёза"
"""
import sys

from db.pending_voices import get_pending_by_id, mark_resolved
from db.voice_profiles import save_voice_profile, get_all_profiles


def main():
    if len(sys.argv) < 3:
        print('Использование: python label_one.py <pending_id> "<ФИО>"')
        print('               python label_one.py <pending_id> "-"')
        sys.exit(1)

    try:
        pid = int(sys.argv[1])
    except ValueError:
        print(f"❌ pending_id должен быть числом: {sys.argv[1]}")
        sys.exit(1)

    name = sys.argv[2].strip()
    pending = get_pending_by_id(pid)
    if not pending:
        print(f"❌ pending #{pid} не найден (уже размечен?)")
        sys.exit(1)

    spk = pending.get("speaker_id") or "?"
    dur = pending.get("duration_sec", 0)
    vid = pending.get("video_id", "")

    if name == "-":
        mark_resolved(pid, "ignored")
        print(f"⏭ pending #{pid} ({spk}, {dur:.0f}с, {vid}) — удалён (ignored)")
        return

    if not name or len(name) < 3 or len(name) > 60:
        print(f"❌ ФИО: 3–60 символов. Получено: {name!r}")
        sys.exit(1)

    save_voice_profile(
        name=name, bio="", role="guest",
        embedding=pending["embedding"],
        duration_sec=pending.get("duration_sec", 0),
        source_video_id=vid,
    )
    mark_resolved(pid, "resolved")
    print(f"✅ pending #{pid} ({spk}, {dur:.0f}с, {vid}) → {name}")
    print(f"📊 В voice_profiles: {len(get_all_profiles())}")


if __name__ == "__main__":
    main()