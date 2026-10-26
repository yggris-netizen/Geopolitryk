"""Воркер: обрабатывает задачи из очереди."""

import asyncio
import gc
import os
import traceback

from config import CHANNELS_TO_WATCH, WORKER_POLL_INTERVAL
from db.queue import (
    _get_next_pending_task, _mark_task_processing,
    _mark_task_done, _handle_task_failure,
)
from db.videos import _mark_video_processed
from publishers.telegram import process_and_publish
from services.youtube import get_video_info_ytdlp, _extract_video_id


async def _worker_loop(shutdown_event: asyncio.Event):
    print("🔧 Воркер запущен")
    while not shutdown_event.is_set():
        try:
            task = _get_next_pending_task()
            if not task:
                await asyncio.sleep(WORKER_POLL_INTERVAL)
                continue

            tid = task['id']
            url = task['video_url']
            attempts_before = task['attempts'] or 0
            attempts_now = attempts_before + 1
            print(f"\n{'▓' * 60}")
            print(f"🔧 ЗАДАЧА #{tid} ({task['source']}, P{task['priority']}, "
                  f"attempts {attempts_before}→{attempts_now}): {url}")
            print(f"{'▓' * 60}")

            _mark_task_processing(tid)
            try:
                info = {}
                if not task['video_title'] or not task['channel_name']:
                    info = await asyncio.to_thread(get_video_info_ytdlp, url)

                video_title = task['video_title'] or info.get('title', '')
                video_published_at = (
                    task.get('video_published_at') or info.get('published_at', '')
                )
                channel_name = task['channel_name'] or info.get('channel', '')
                channel_type = task['channel_type'] or 'media'
                channel_owner = task['channel_owner'] or ''

                cn_low = (channel_name or '').lower().strip()
                if cn_low in ('виталий дикий', 'виталий дикий live'):
                    channel_name = "Дикий live"

                if channel_name:
                    cn_low = channel_name.lower().strip()
                    matched = False
                    for ch in CHANNELS_TO_WATCH:
                        ch_name_low = ch['name'].lower().strip()
                        ch_owner_low = (ch.get('owner', '') or '').lower().strip()
                        if (cn_low == ch_name_low
                                or cn_low in ch_name_low
                                or ch_name_low in cn_low
                                or (ch_owner_low and cn_low == ch_owner_low)):
                            channel_name = ch['name']
                            channel_type = ch.get('type', 'media')
                            channel_owner = ch.get('owner', '')
                            matched = True
                            break
                    if not matched:
                        channel_type = "media"
                        channel_owner = ""

                ok, reason = await process_and_publish(
                    url,
                    video_title=video_title,
                    video_description=info.get('description', ''),
                    video_duration=info.get('duration', 0),
                    video_published_at=video_published_at,
                    channel_name=channel_name,
                    channel_type=channel_type,
                    channel_owner=channel_owner,
                    skip_duration_check=(task['source'] == 'telegram'),
                )

                if ok:
                    _mark_task_done(tid)
                    vid = task['video_id'] or _extract_video_id(url)
                    if vid:
                        _mark_video_processed(vid)
                    print(f"✅ #{tid} OK")
                else:
                    _handle_task_failure(tid, reason or "unknown", attempts_now)

            except Exception as e:
                err_text = f"{type(e).__name__}: {e}"
                print(f"❌ #{tid}: {err_text}")
                traceback.print_exc()
                try:
                    import sys
                    sys.stderr.flush()
                except Exception:
                    pass
                _handle_task_failure(tid, err_text, attempts_now)

            try:
                gc.collect()
            except Exception:
                pass

        except Exception as e:
            print(f"⚠️  воркер: {e}")
            traceback.print_exc()
            await asyncio.sleep(WORKER_POLL_INTERVAL)

    print("🛑 Воркер завершён")