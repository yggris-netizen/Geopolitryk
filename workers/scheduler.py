"""Планировщик: автопроверка каналов и heartbeat."""

import os
import random
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    CHANNELS_TO_WATCH, CHECK_INTERVAL_HOURS, HEARTBEAT_HOURS,
    MAX_VIDEOS_PER_CHANNEL_PER_CHECK, MAX_REPEAT_EXPERTS_PER_CYCLE,
    OWNER_USER_ID, YOUTUBE_DAILY_QUOTA,
)
from db.queue import (
    _add_task_to_queue, _queue_stats, _cleanup_done_tasks,
)
from db.experts import (
    _get_experts_seen_today, _cleanup_old_experts,
)
from db.videos import _mark_video_processed
from services.expert_detection import (
    _extract_name_from_title, _extract_name_from_description,
    _finalize_name, _is_owner_name,
)
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
    except Exception:
        pass

    channels_shuffled = CHANNELS_TO_WATCH.copy()
    random.shuffle(channels_shuffled)

    seen_experts = _get_experts_seen_today()
    if seen_experts:
        print(f"\n👥 Уже выступали сегодня ({len(seen_experts)}):")
        for e in sorted(seen_experts):
            print(f"   • {e}")

    repeat_count = {}
    added = 0
    skipped = 0

    for ch in channels_shuffled:
        if shutdown_event is not None and shutdown_event.is_set():
            print("⏹ Shutdown — прерываю автоцикл")
            break
        print(f"\n📺 {ch['name']}")
        try:
            videos = get_new_videos_from_channel(ch["id"])
        except Exception as e:
            print(f"   ❌ {e}")
            continue
        if not videos:
            print("   новых нет")
            continue
        if len(videos) > MAX_VIDEOS_PER_CHANNEL_PER_CHECK:
            print(f"   ⚠️  ограничиваю до {MAX_VIDEOS_PER_CHANNEL_PER_CHECK}")
            videos = videos[:MAX_VIDEOS_PER_CHANNEL_PER_CHECK]

        for v in videos:
            prelim = _extract_name_from_title(v["title"])
            if not prelim:
                prelim = _extract_name_from_description(v.get("description", ""))
            if prelim:
                prelim = _finalize_name(prelim)

            owner = ch.get("owner", "") or ""
            if ch.get("type") == "author" and owner and prelim:
                if _is_owner_name(prelim, owner):
                    prelim = ""

            is_rep = prelim and prelim in seen_experts
            prio = 0 if is_rep else 5

            if is_rep:
                cnt = repeat_count.get(prelim, 0)
                if cnt >= MAX_REPEAT_EXPERTS_PER_CYCLE:
                    print(f"   ⏭  лимит ({prelim}): {v['title'][:50]}")
                    _mark_video_processed(v["video_id"])
                    skipped += 1
                    continue
                repeat_count[prelim] = cnt + 1

            tid = _add_task_to_queue(
                video_url=v["url"], video_id=v["video_id"],
                video_title=v["title"],
                video_published_at=v.get("published_at", ""),
                channel_name=ch["name"],
                channel_type=ch.get("type", "media"),
                channel_owner=ch.get("owner", ""),
                priority=prio, source="auto",
            )
            if tid:
                tag = "🔁" if is_rep else "🆕"
                print(f"   {tag} #{tid}: {v['title'][:60]}")
                added += 1
            else:
                print(f"   ⏭  в очереди: {v['title'][:60]}")

    q = _quota_info()
    print(f"\n📊 Автоцикл: +{added}, пропущено {skipped}")
    print(f"📊 Квота YouTube: {q['count']}/{YOUTUBE_DAILY_QUOTA} ({q['by_op']})")
    print(f"{'=' * 60}")


async def _heartbeat_job(bot):
    try:
        s = _queue_stats()
        q = _quota_info()
        text = (f"💚 <b>Heartbeat</b>\n\n"
                f"⏳ {s.get('pending', 0)} | 🔄 {s.get('processing', 0)} | "
                f"✅ {s.get('done', 0)} | ❌ {s.get('error', 0)}\n"
                f"📊 Квота: {q['count']}/{YOUTUBE_DAILY_QUOTA}")
        if PSUTIL_AVAILABLE:
            try:
                mem_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                text += f"\n💾 RAM: {mem_mb:.0f} MB"
            except Exception:
                pass
        await bot.send_message(chat_id=OWNER_USER_ID, text=text)
    except Exception as e:
        print(f"⚠️  heartbeat: {e}")


def build_scheduler(bot, shutdown_event):
    scheduler = AsyncIOScheduler()

    async def _check_job():
        await check_all_channels(shutdown_event)

    scheduler.add_job(_check_job, "interval", hours=CHECK_INTERVAL_HOURS)

    if HEARTBEAT_HOURS > 0:
        async def _hb_job():
            await _heartbeat_job(bot)
        scheduler.add_job(_hb_job, "interval", hours=HEARTBEAT_HOURS)

    return scheduler