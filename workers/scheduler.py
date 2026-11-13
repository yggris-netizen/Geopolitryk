"""Планировщик: автопроверка каналов и heartbeat."""

import os
import random
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    CHANNELS_TO_WATCH, CHECK_INTERVAL_HOURS, HEARTBEAT_HOURS,
    MAX_VIDEOS_PER_CHANNEL_PER_CHECK,
    OWNER_USER_ID, TRANSCRIPTS_CACHE_DAYS, YOUTUBE_DAILY_QUOTA,
    FACT_CHECK_ENABLED, FACT_CHECK_DAY,
    FACT_CHECK_HOUR_UTC, FACT_CHECK_MINUTE_UTC,
)
from db.queue import (
    _add_task_to_queue, _queue_stats, _cleanup_done_tasks,
)
from db.experts import (
    _get_experts_seen_today, _cleanup_old_experts,
)
from db.transcripts import cleanup_old_transcripts
from services.youtube import (
    get_new_videos_from_channel, _quota_info,
)

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


async def check_all_channels(shutdown_event=None):
    if not CHANNELS_TO_WATCH:
        return
    print(f"\n{'=' * 60}")
    print(f"АВТОПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 60}")

    try:
        _cleanup_old_experts(days_keep=30)
        _cleanup_done_tasks(days_keep=3)
        cleanup_old_transcripts(days_keep=TRANSCRIPTS_CACHE_DAYS)
    except Exception:
        pass

    channels_shuffled = CHANNELS_TO_WATCH.copy()
    random.shuffle(channels_shuffled)

    seen_experts = _get_experts_seen_today()
    if seen_experts:
        print(f"\n👥 Уже выступали сегодня ({len(seen_experts)}):")
        for e in sorted(seen_experts):
            print(f"   • {e}")

    added = 0

    for ch in channels_shuffled:
        if shutdown_event is not None and shutdown_event.is_set():
            print("[STOP] Shutdown — прерываю автоцикл")
            break
        print(f"\n[TV] {ch['name']}")
        try:
            videos = get_new_videos_from_channel(ch["id"])
        except Exception as e:
            print(f"   [ERR] {e}")
            continue
        if not videos:
            print("   новых нет")
            continue
        if len(videos) > MAX_VIDEOS_PER_CHANNEL_PER_CHECK:
            print(f"   [WARN]  ограничиваю до {MAX_VIDEOS_PER_CHANNEL_PER_CHECK}")
            videos = videos[:MAX_VIDEOS_PER_CHANNEL_PER_CHECK]

        for v in videos:
            tid = _add_task_to_queue(
                video_url=v["url"], video_id=v["video_id"],
                video_title=v["title"],
                video_published_at=v.get("published_at", ""),
                channel_name=ch["name"],
                priority=5, source="auto",
            )
            if tid:
                print(f"   [NEW] #{tid}: {v['title'][:60]}")
                added += 1
            else:
                print(f"   [SKIP]  в очереди: {v['title'][:60]}")

    q = _quota_info()
    print(f"\n[STAT] Автоцикл: +{added}")
    print(f"[STAT] Квота YouTube: {q['count']}/{YOUTUBE_DAILY_QUOTA} ({q['by_op']})")
    print(f"{'=' * 60}")


async def _heartbeat_job(bot):
    try:
        s = _queue_stats()
        q = _quota_info()
        text = (f"💚 <b>Heartbeat</b>\n\n"
                f"[WAIT] {s.get('pending', 0)} | [RETRY] {s.get('processing', 0)} | "
                f"[OK] {s.get('done', 0)} | [ERR] {s.get('error', 0)}\n"
                f"[STAT] Квота: {q['count']}/{YOUTUBE_DAILY_QUOTA}")
        if PSUTIL_AVAILABLE:
            try:
                mem_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                text += f"\n[SAVE] RAM: {mem_mb:.0f} MB"
            except Exception:
                pass
        await bot.send_message(chat_id=OWNER_USER_ID, text=text)
    except Exception as e:
        print(f"[WARN]  heartbeat: {e}")


def build_scheduler(bot, shutdown_event):
    scheduler = AsyncIOScheduler()

    async def _check_job():
        await check_all_channels(shutdown_event)

    scheduler.add_job(_check_job, "interval", hours=CHECK_INTERVAL_HOURS)

    if HEARTBEAT_HOURS > 0:
        async def _hb_job():
            await _heartbeat_job(bot)
        scheduler.add_job(_hb_job, "interval", hours=HEARTBEAT_HOURS)

    if FACT_CHECK_ENABLED:
        from workers.fact_check_worker import fact_check_job
        async def _fc_job():
            await fact_check_job(bot)
        scheduler.add_job(
            _fc_job, "cron",
            day_of_week=FACT_CHECK_DAY,
            hour=FACT_CHECK_HOUR_UTC,
            minute=FACT_CHECK_MINUTE_UTC,
        )
        print(f"[FACT] Планировщик: фактчекинг ({FACT_CHECK_DAY} "
              f"{FACT_CHECK_HOUR_UTC:02d}:{FACT_CHECK_MINUTE_UTC:02d} UTC)")

    return scheduler