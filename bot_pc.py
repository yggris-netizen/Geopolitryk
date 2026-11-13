# -*- coding: utf-8 -*-
"""bot_pc.py - Downloader (ПК, РФ): pending -> download -> rclone upload -> uploaded.

БЕЗ aiogram, БЕЗ publishers/telegram, БЕЗ commentary. Лягушка на ПК.
"""
import asyncio
import os
import signal
import sys
import traceback

from config import (
    CHANNELS_TO_WATCH, WORKER_POLL_INTERVAL, DEBUG_BOOT, DB_PATH,
)
from db.connection import _init_db, _cleanup_temp_files
from db.queue import (
    _get_next_pending_task, _mark_task_processing,
    _mark_task_uploaded, _handle_task_failure,
    _queue_list, _recent_errors, _reset_queue,
)
from services.transcription import download_audio
from services.rclone_transfer import upload_audio
from services.youtube import get_video_info_ytdlp, _extract_video_id
from workers.scheduler import check_all_channels


_SHUTDOWN_EVENT = None


def _dbg_boot(*args, **kwargs):
    if DEBUG_BOOT:
        print(*args, **kwargs)


async def _worker_loop_pc(shutdown_event):
    print("[PC] Downloader-воркер запущен")
    while not shutdown_event.is_set():
        try:
            task = _get_next_pending_task()
            if not task:
                await asyncio.sleep(WORKER_POLL_INTERVAL)
                continue

            tid = task['id']
            url = task['video_url']
            vid = task['video_id'] or _extract_video_id(url) or ""
            attempts_before = task['attempts'] or 0
            attempts_now = attempts_before + 1
            print(f"\n{'▓' * 60}")
            print(f"[PC] ЗАДАЧА #{tid} (P{task['priority']}, attempts {attempts_before}→{attempts_now}): {url}")
            print(f"{'▓' * 60}")

            _mark_task_processing(tid)
            audio_file = None
            try:
                if not vid:
                    await asyncio.to_thread(get_video_info_ytdlp, url)
                    vid = _extract_video_id(url) or ""
                print("[PC] Шаг 1/2: Скачивание...")
                audio_file = await asyncio.to_thread(download_audio, url)
                print(f"[PC] Скачано: {audio_file}")
                print("[PC] Шаг 2/2: rclone upload...")
                remote_path = await asyncio.to_thread(upload_audio, audio_file, vid)
                _mark_task_uploaded(tid, remote_path)
                print(f"[PC] #{tid} -> uploaded")
            except Exception as e:
                err_text = f"{type(e).__name__}: {e}"
                print(f"[PC] [ERR] #{tid}: {err_text}")
                traceback.print_exc()
                _handle_task_failure(tid, err_text, attempts_now)
            finally:
                if audio_file and os.path.exists(audio_file):
                    try:
                        os.remove(audio_file)
                        print(f"[PC] 🧹 аудио удалено: {audio_file}")
                    except Exception as e:
                        print(f"[PC] [WARN] cleanup: {e}")
        except Exception as e:
            print(f"[PC] [WARN] воркер: {e}")
            traceback.print_exc()
            await asyncio.sleep(WORKER_POLL_INTERVAL)
    print("[PC] Воркер завершён")


async def _main():
    global _SHUTDOWN_EVENT
    _SHUTDOWN_EVENT = asyncio.Event()

    print("=" * 60)
    print("[PC] ЗАПУСК DOWNLOADER (ПК)")
    print("=" * 60)
    print(f"   Каналов: {len(CHANNELS_TO_WATCH)}")
    print(f"   БД: {DB_PATH}")
    print()

    if "--queue" in sys.argv:
        _init_db()
        for t in _queue_list():
            print(f"[WAIT] #{t['id']} [P{t['priority']}] ({t['status']}) "
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

    if "--process" in sys.argv:
        _init_db()
        idx = sys.argv.index("--process")
        try:
            url = sys.argv[idx + 1]
        except IndexError:
            print("python bot_pc.py --process <url>")
            return
        info = get_video_info_ytdlp(url)
        vid = _extract_video_id(url) or ""
        print(f"[PC] --process: {url}")
        try:
            audio_file = await asyncio.to_thread(download_audio, url)
            remote_path = await asyncio.to_thread(upload_audio, audio_file, vid)
            print(f"[PC] upload OK: {remote_path}")
            os.remove(audio_file)
        except Exception as e:
            print(f"[PC] [ERR] --process: {e!r}")
        return

    _cleanup_temp_files()
    _init_db()

    try:
        from workers.pubsub_listener import start_listener_thread
        start_listener_thread()
        print("[PC] PubSubHubbub listener запущен")
    except Exception as e:
        print(f"[PC] [WARN] listener: {e}")

    await check_all_channels(_SHUTDOWN_EVENT)

    worker_task = asyncio.create_task(_worker_loop_pc(_SHUTDOWN_EVENT))

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: _SHUTDOWN_EVENT.set())
            except Exception:
                pass
    except Exception:
        pass

    print("\n[PC] ГОТОВ. Ctrl+C для остановки.\n")
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    finally:
        _SHUTDOWN_EVENT.set()
        print("[PC] Остановлено")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n[PC] Остановлено (Ctrl+C)")
