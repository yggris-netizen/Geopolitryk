# -*- coding: utf-8 -*-
"""bot_server.py - Processor+Publisher (сервер): uploaded -> processed -> published.

С aiogram (Publisher), с fact_check_worker (cron). БЕЗ лягушки.
"""
import asyncio
import os
import sys
import traceback

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import (
    BOT_TOKEN, CHANNEL_ID, DB_PATH, OWNER_USER_ID,
    WORKER_POLL_INTERVAL, CHECK_INTERVAL_HOURS, DEBUG_BOOT,
    GROQ_MODEL, GROQ_WHISPER_MODEL, WHISPER_MODE,
)
from db.connection import _init_db, _cleanup_temp_files
from db.queue import (
    _get_next_task_by_status, _mark_task_processing,
    _mark_task_processed, _mark_task_published,
    _handle_task_failure,
    _queue_list, _recent_errors, _reset_queue,
)
from db.videos import _mark_video_processed
from handlers import (
    admin_router, links_router, predictions_router,
    voice_registration_router,
)
from handlers.admin import set_owner_user_id as set_admin_owner
from handlers.links import set_owner_user_id as set_links_owner
from handlers.predictions import set_owner_user_id as set_preds_owner
from publishers.telegram import process_and_publish, set_bot
from services.rclone_transfer import download_audio as rclone_download
from services.youtube import _extract_video_id
from workers.scheduler import build_scheduler


_SHUTDOWN_EVENT = None
AUDIO_TEMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_temp")


def _dbg_boot(*args, **kwargs):
    if DEBUG_BOOT:
        print(*args, **kwargs)


async def _processor_loop(shutdown_event):
    os.makedirs(AUDIO_TEMP, exist_ok=True)
    print("[SRV] Processor-воркер запущен")
    while not shutdown_event.is_set():
        try:
            task = _get_next_task_by_status('uploaded')
            if not task:
                await asyncio.sleep(WORKER_POLL_INTERVAL)
                continue

            tid = task['id']
            url = task['video_url']
            vid = task['video_id'] or _extract_video_id(url) or ""
            remote_name = f"{vid}.mp3"
            local_audio = os.path.join(AUDIO_TEMP, remote_name)
            attempts_before = task['attempts'] or 0
            attempts_now = attempts_before + 1
            print(f"\n{'▓' * 60}")
            print(f"[SRV] ЗАДАЧА #{tid} (attempts {attempts_before}→{attempts_now}): {url}")
            print(f"{'▓' * 60}")

            _mark_task_processing(tid)
            try:
                print(f"[SRV] rclone download: {remote_name}")
                ok_dl = await asyncio.to_thread(rclone_download, remote_name, local_audio)
                if not ok_dl or not os.path.exists(local_audio):
                    raise RuntimeError("rclone download failed")

                print(f"[SRV] process_and_publish (audio_file_override={local_audio})...")
                ok, reason = await process_and_publish(
                    url,
                    video_title=task['video_title'] or "",
                    video_description="",
                    video_duration=0,
                    video_published_at=task.get('video_published_at') or "",
                    channel_name=task['channel_name'] or "",
                    channel_type=task.get('channel_type') or "media",
                    channel_owner=task.get('channel_owner') or "",
                    skip_duration_check=True,
                    audio_file_override=local_audio,
                )
                if ok:
                    _mark_task_processed(tid)
                    _mark_task_published(tid)
                    if vid:
                        _mark_video_processed(vid)
                    print(f"[SRV] #{tid} -> published")
                else:
                    _handle_task_failure(tid, reason or "unknown", attempts_now)
            except Exception as e:
                err_text = f"{type(e).__name__}: {e}"
                print(f"[SRV] [ERR] #{tid}: {err_text}")
                traceback.print_exc()
                _handle_task_failure(tid, err_text, attempts_now)
            finally:
                if os.path.exists(local_audio):
                    try:
                        os.remove(local_audio)
                        print(f"[SRV] 🧹 аудио удалено: {local_audio}")
                    except Exception as e:
                        print(f"[SRV] [WARN] cleanup: {e}")
        except Exception as e:
            print(f"[SRV] [WARN] воркер: {e}")
            traceback.print_exc()
            await asyncio.sleep(WORKER_POLL_INTERVAL)
    print("[SRV] Воркер завершён")


async def _main():
    global _SHUTDOWN_EVENT
    _SHUTDOWN_EVENT = asyncio.Event()

    print("=" * 60)
    print("[SRV] ЗАПУСК PROCESSOR+PUBLISHER (сервер)")
    print("=" * 60)
    print(f"   БД: {DB_PATH}")
    print(f"   Whisper: {WHISPER_MODE} ({GROQ_WHISPER_MODEL})")
    print()

    if "--queue" in sys.argv:
        _init_db()
        for t in _queue_list(statuses=('pending', 'processing', 'uploaded', 'processed')):
            print(f"[WAIT] #{t['id']} [{t['status']}] "
                  f"{(t['video_title'] or t['video_url'])[:60]}")
        return

    if "--errors" in sys.argv:
        _init_db()
        errs = _recent_errors(20)
        for e in errs:
            print(f"[ERR] #{e['id']}: {e['error_message'] or '—'}")
        return

    if "--reset" in sys.argv:
        _init_db()
        n = _reset_queue()
        print(f"Сброшено: {n}")
        return

    _cleanup_temp_files()
    _init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(admin_router, predictions_router, links_router, voice_registration_router)
    set_admin_owner(OWNER_USER_ID)
    set_links_owner(OWNER_USER_ID)
    set_preds_owner(OWNER_USER_ID)
    set_bot(bot)

    worker_task = asyncio.create_task(_processor_loop(_SHUTDOWN_EVENT))
    scheduler = build_scheduler(bot, _SHUTDOWN_EVENT)
    scheduler.start()

    print(f"\n{'=' * 60}")
    print("[SRV] ГОТОВ")
    print("   Processor: запущен")
    print(f"   Scheduler: каждые {CHECK_INTERVAL_HOURS} ч + fact_check (cron)")
    print(f"   БД: {DB_PATH}")
    print(f"{'=' * 60}\n")

    try:
        await dp.start_polling(bot)
    finally:
        print("\n[SRV] Останавливаюсь...")
        _SHUTDOWN_EVENT.set()
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            await asyncio.wait_for(worker_task, timeout=600)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            worker_task.cancel()
        try:
            await bot.session.close()
        except Exception:
            pass
        print("[SRV] Завершено")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n[SRV] Остановлено (Ctrl+C)")
    except BaseException as e:
        print(f"\n[SRV] КРИТИЧЕСКАЯ: {type(e).__name__}: {e}")
        traceback.print_exc()
