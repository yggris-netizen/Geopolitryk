# -*- coding: utf-8 -*-
"""Список pending_voices с заголовками, таймкодами и ссылками на фрагмент."""
import sqlite3

from config import DB_PATH
from db.pending_voices import get_pending
from db.transcripts_raw import get_transcript_raw_segments


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


def _get_first_start(video_id: str, speaker_id: str) -> int:
    """Возвращает начало первого сегмента с этим speaker_id (в секундах)."""
    if not video_id or not speaker_id:
        return 0
    segs = get_transcript_raw_segments(video_id)
    for s in segs:
        if s.get("speaker") == speaker_id:
            try:
                return int(float(s.get("start", 0)))
            except (TypeError, ValueError):
                return 0
    return 0


def _format_tc(seconds: int) -> str:
    """Секунды → HH:MM:SS или MM:SS."""
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def main():
    items = get_pending(limit=50)
    if not items:
        print("📭 Pending пуст")
        return

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
            start_sec = _get_first_start(vid, spk)
            tc = _format_tc(start_sec)
            frag_url = f"https://youtu.be/{vid}?t={start_sec}" if vid else url
            print(f"  id={pid}, {spk}, {dur:.0f}с, ⏱ {tc}")
            print(f"    🎧 {frag_url}")
            print(f"    → python label_one.py {pid} \"<ФИО>\"")
            print(f"    → python label_one.py {pid} \"-\"   (удалить)")
        print()


if __name__ == "__main__":
    main()