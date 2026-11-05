# -*- coding: utf-8 -*-
"""Список pending_voices с заголовками и готовыми командами для label_one.py."""
import sqlite3

from config import DB_PATH
from db.pending_voices import get_pending


def _get_video_title(video_id: str) -> str:
    if not video_id:
        return ""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT video_title FROM video_summaries WHERE video_id = ? LIMIT 1",
            (video_id,)
        ).fetchone()
        if row and row["video_title"]:
            return row["video_title"]
        row = conn.execute(
            "SELECT video_title FROM task_queue WHERE video_id = ? LIMIT 1",
            (video_id,)
        ).fetchone()
        if row and row["video_title"]:
            return row["video_title"]
    except Exception:
        pass
    finally:
        conn.close()
    return ""


def main():
    items = get_pending(limit=50)
    if not items:
        print("📭 Pending пуст")
        return

    # Группируем по video_id
    by_video = {}
    for p in items:
        vid = p.get("video_id", "")
        by_video.setdefault(vid, []).append(p)

    print(f"📋 Pending: {len(items)} из {len(by_video)} видео\n")

    for vid, plist in by_video.items():
        title = _get_video_title(vid)
        title_str = title[:80] if title else "(заголовок не найден)"
        url = plist[0].get("video_url", "") if plist else ""
        print(f"{'=' * 70}")
        print(f"📺 {vid} — {title_str}")
        print(f"🔗 {url}")
        print(f"{'=' * 70}")
        for p in plist:
            pid = p["id"]
            spk = p.get("speaker_id") or "?"
            dur = p.get("duration_sec", 0)
            print(f"  id={pid}, {spk}, {dur:.0f}с")
            print(f"    → python label_one.py {pid} \"<ФИО>\"")
            print(f"    → python label_one.py {pid} \"-\"   (удалить)")
        print()


if __name__ == "__main__":
    main()