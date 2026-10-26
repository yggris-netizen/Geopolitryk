"""
YouTube → Telegram бот: аналитические выжимки.
Единая очередь + Telegram-управление + воркер.

Этап 2 декомпозиции:
  - YouTube → services/youtube.py
  - Транскрибация → services/transcription.py
  - Эксперты → services/expert_detection.py
  - Суммаризация → services/summarization.py
  - Прогнозы → services/predictions_extraction.py
  - Groq-клиент → services/llm.py
  - YouTube API → services/youtube_api.py
"""

import asyncio
import html
import json
import os
import random
import re
import signal
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception as _e:
        print(f"⚠️  WindowsSelectorEventLoopPolicy: {_e}")

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("⚠️  psutil не установлен")

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from experts import KNOWN_EXPERT_BIOS, KNOWN_EXPERT_FULL

from config import *
from db.connection import _init_db, _cleanup_temp_files
from db.queue import (
    _add_task_to_queue, _get_next_pending_task, _mark_task_processing,
    _mark_task_done, _mark_task_retry, _handle_task_failure,
    _cancel_task, _reset_queue, _queue_stats, _queue_list,
    _recent_errors, _cleanup_done_tasks,
)
from db.predictions import (
    _save_predictions, _recent_predictions, get_expert_priority_rating,
)
from db.summaries import _save_summary
from db.experts import (
    _get_today_msk, _get_experts_seen_today,
    _mark_expert_seen_today, _cleanup_old_experts,
)
from db.videos import _is_video_processed, _mark_video_processed

from services.llm import groq_client
from services.youtube_api import youtube
from services.youtube import (
    _get_uploads_playlist_id, _get_channel_videos_rss, _get_channel_videos_raw,
    _parse_iso_duration, _get_video_durations, _format_duration,
    _format_published_date, _fetch_videos_from_channel, get_new_videos_from_channel,
    get_video_info_ytdlp, _extract_video_id, _get_thumbnail_url,
    _is_youtube_url, _extract_youtube_url,
    _maybe_trim_caches, _quota_tick, _quota_info, _is_quota_error,
    _YOUTUBE_UPLOADS_CACHE, _YOUTUBE_VIDEOS_CACHE, _YOUTUBE_DURATIONS_CACHE,
)
from services.transcription import (
    download_audio, _transcribe_groq, _transcribe_local, transcribe_audio,
)
from services.expert_detection import (
    _sanitize_expert_name, _finalize_name, _extract_name_from_title,
    _extract_name_from_description, _is_owner_name,
    detect_expert_name, generate_expert_bio,
)
from services.summarization import (
    neutralize_title, _simple_neutralize, _is_title_neutral,
    generate_hashtags, _call_groq_with_retry, _summarize_chunk, summarize_text,
    _check_context_errors, _format_title,
)
from services.predictions_extraction import extract_predictions


_SHUTDOWN_EVENT = None


# ╔══════════════════════════════════════════════════════════╗
# ║         ДИАГНОСТИКА                                      ║
# ╚══════════════════════════════════════════════════════════╝

def _log_memory(stage: str = ""):
    if not PSUTIL_AVAILABLE:
        return
    try:
        p = psutil.Process(os.getpid())
        mem_mb = p.memory_info().rss / 1024 / 1024
        print(f"   💾 [{stage}] RAM: {mem_mb:.0f} MB")
    except Exception:
        pass


# ╔══════════════════════════════════════════════════════════╗
# ║              ИНИЦИАЛИЗАЦИЯ                               ║
# ╚══════════════════════════════════════════════════════════╝

print("=" * 60)
print("ЗАПУСК БОТА")
print("=" * 60)

_errors = []
if not BOT_TOKEN:
    _errors.append("❌ BOT_TOKEN не задан")
if not GROQ_API_KEY:
    _errors.append("❌ GROQ_API_KEY не задан")
if not isinstance(CHANNEL_ID, int) or CHANNEL_ID >= 0:
    _errors.append("❌ CHANNEL_ID должен быть отрицательным")
if not isinstance(OWNER_USER_ID, int) or OWNER_USER_ID <= 0:
    _errors.append("❌ OWNER_USER_ID должен быть положительным")
if not os.path.exists(SERVICE_ACCOUNT_FILE):
    _errors.append(f"❌ {SERVICE_ACCOUNT_FILE} не найден")
if WHISPER_MODE not in ("groq", "local"):
    _errors.append(f"❌ WHISPER_MODE: '{WHISPER_MODE}'")

for _ch in CHANNELS_TO_WATCH:
    if _ch["id"] != _ch["id"].strip():
        _errors.append(f"❌ ID «{_ch['name']}» содержит пробелы")

if _errors:
    print("\n⚠️  ОШИБКИ:\n")
    for _e in _errors:
        print(f"  {_e}")
    sys.exit(1)

print("✅ Настройки в порядке")
print(f"   Канал:            {CHANNEL_ID}")
print(f"   Owner:            {OWNER_USER_ID}")
print(f"   Модель LLM:       {GROQ_MODEL}")
print(f"   Режим Whisper:    {WHISPER_MODE}")
if WHISPER_MODE == "groq":
    print(f"   Groq Whisper:     {GROQ_WHISPER_MODEL}")
    print(f"   Нарезка:          {GROQ_MAX_CHUNK_SECONDS}с")
    print(f"   Лимит файла:      {GROQ_MAX_SIZE_MB} MB")
else:
    print(f"   Локальная:        {WHISPER_MODEL_SIZE}")
print(f"   Каналов:          {len(CHANNELS_TO_WATCH)}")
print(f"   Глубина поиска:   {HOURS_BACK} ч (без фолбэка)")
print(f"   Лимит видео/канал: {MAX_VIDEOS_PER_CHANNEL_PER_CHECK}")
print(f"   Max attempts:     {MAX_TASK_ATTEMPTS}")
print(f"   БД:               {DB_PATH}")
print(f"   Словарь экспертов: {len(KNOWN_EXPERT_BIOS)} записей, "
      f"{len(KNOWN_EXPERT_FULL)} расширений")
if HEARTBEAT_HOURS > 0:
    print(f"   Heartbeat:        каждые {HEARTBEAT_HOURS} ч")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
print()


# ╔══════════════════════════════════════════════════════════╗
# ║              ПУБЛИКАЦИЯ                                  ║
# ╚══════════════════════════════════════════════════════════╝

async def send_expert_post(video_url, video_title, expert_name, expert_bio,
                            channel_name, summary, published_at=""):
    video_id = _extract_video_id(video_url)
    date_str = _format_published_date(published_at)

    parts = []
    if video_title:
        parts.append(_format_title(video_title))
    el = f"<b>ЭКСПЕРТ</b>\n{expert_name}"
    if expert_bio:
        el += f" — {expert_bio}"
    parts.append(el)
    if channel_name and channel_name.strip().lower() != expert_name.strip().lower():
        parts.append(f"<b>КАНАЛ</b>\n{channel_name}")
    if date_str:
        parts.append(f"<b>ОПУБЛИКОВАНО</b>\n{date_str}")
    parts.append("<i>Выжимка ниже ↓</i>")
    caption = "\n\n".join(parts)
    if len(caption) > 1024:
        caption = caption[:1020] + "…"

    sent = False
    url = _get_thumbnail_url(video_id)
    if url:
        try:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=url,
                                 caption=caption, parse_mode=ParseMode.HTML)
            print("✅ Фото")
            sent = True
        except Exception as e:
            print(f"⚠️  Фото: {e}")
    if not sent:
        await bot.send_message(chat_id=CHANNEL_ID, text=caption)

    hashtags = generate_hashtags(expert_name, summary) if GENERATE_HASHTAGS else ""
    text = f"{summary}\n\n🔗 <a href='{video_url}'>Смотреть оригинал</a>\n"
    if hashtags:
        text += f"\n{hashtags}\n"
    text += f"\n<i>Сгенерировано автоматически.</i>"

    if len(text) <= TELEGRAM_MAX_LEN:
        await bot.send_message(chat_id=CHANNEL_ID, text=text)
        print("✅ Выжимка")
    else:
        print(f"✂️  Текст {len(text)} > {TELEGRAM_MAX_LEN}, разбиваю...")
        parts = []
        current = ""
        for paragraph in text.split("\n\n"):
            if len(current) + len(paragraph) + 2 <= TELEGRAM_MAX_LEN:
                current = (current + "\n\n" + paragraph).strip() if current else paragraph
            else:
                if current:
                    parts.append(current)
                if len(paragraph) > TELEGRAM_MAX_LEN:
                    sentences = re.split(r'(?<=[.!?])\s+', paragraph)
                    buf = ""
                    for s in sentences:
                        if len(buf) + len(s) + 1 <= TELEGRAM_MAX_LEN:
                            buf = (buf + " " + s).strip() if buf else s
                        else:
                            if buf:
                                parts.append(buf)
                            buf = s[:TELEGRAM_MAX_LEN]
                    if buf:
                        parts.append(buf)
                else:
                    current = paragraph
        if current:
            parts.append(current)

        for i, part in enumerate(parts, 1):
            header = f"<i>(часть {i}/{len(parts)})</i>\n\n" if len(parts) > 1 else ""
            await bot.send_message(chat_id=CHANNEL_ID, text=header + part)
            print(f"✅ Выжимка {i}/{len(parts)}")
            await asyncio.sleep(1)


async def process_and_publish(video_url, video_title="", video_description="",
                               video_duration=0, video_published_at="",
                               channel_name="", channel_type="media",
                               channel_owner="", skip_duration_check=False) -> tuple:
    print(f"\n{'=' * 60}")
    print(f"ОБРАБОТКА: {video_url}")
    if video_title:
        print(f"  Заголовок: {video_title[:80]}")
    if channel_name:
        print(f"  Канал: {channel_name} [{channel_type}]")
    print(f"{'=' * 60}\n")

    audio_file = None
    try:
        duration = video_duration
        published_at = video_published_at

        if not video_title:
            print("🔍 yt-dlp...")
            info = await asyncio.to_thread(get_video_info_ytdlp, video_url)
            video_title = info.get("title", "")
            video_description = info.get("description", "")
            duration = info.get("duration", 0)
            if not published_at:
                published_at = info.get("published_at", "")
            if not channel_name:
                channel_name = info.get("channel", "")
            print(f"  Длительность: {_format_duration(duration)}\n")

            if not skip_duration_check and duration > 0:
                if duration < MIN_VIDEO_DURATION_SECONDS:
                    return False, f"too_short: {duration}s"
                if duration > MAX_VIDEO_DURATION_SECONDS:
                    return False, f"too_long: {duration}s"

        if not published_at:
            print("📅 Дата не задана — загружаю...")
            extra = await asyncio.to_thread(get_video_info_ytdlp, video_url)
            published_at = extra.get("published_at", "")

        print("📥 Шаг 1/4: Скачивание...")
        _log_memory("до скачивания")
        audio_file = await asyncio.to_thread(download_audio, video_url)
        print("✅ Скачано\n")
        _log_memory("после скачивания")

        print("🎤 Шаг 2/4: Транскрибация...")
        transcript = await asyncio.to_thread(transcribe_audio, audio_file, duration)
        audio_file = None

        transcript_len = len(transcript.strip())
        print(f"📊 Длина транскрипта: {transcript_len}")
        print(f"📊 Начало: {transcript[:200]!r}")

        if transcript_len < MIN_TRANSCRIPT_CHARS:
            return False, f"transcript_too_short: {transcript_len}"

        print(f"✅ ({transcript_len})\n")

        if DIAGNOSTIC_MODE:
            print("🔧 ДИАГНОСТИКА: транскрибация прошла")
            return True, f"diagnostic_ok: {transcript_len}"

        display_title = video_title
        if NEUTRALIZE_TITLE and video_title:
            print("🎨 Нейтрализация...")
            n = neutralize_title(video_title)
            if n and n != video_title:
                display_title = n
                print(f"✅ {display_title}\n")

        print("🔍 Эксперт...")
        expert_name = detect_expert_name(
            video_title, transcript, description=video_description,
            channel_name=channel_name, channel_type=channel_type,
            channel_owner=channel_owner,
        )
        print(f"✅ {expert_name}\n")

        print("📝 Специализация...")
        expert_bio = generate_expert_bio(expert_name, transcript)
        if expert_bio:
            print(f"✅ {expert_bio}\n")
        else:
            print("⚠️  нет\n")

        print("🧠 Шаг 3/4: Суммаризация...")
        summary = summarize_text(transcript)
        summary = _check_context_errors(summary, transcript)
        print("✅ Готово\n")

        print("📤 Шаг 4/4: Отправка...")
        await send_expert_post(
            video_url=video_url, video_title=display_title,
            expert_name=expert_name, expert_bio=expert_bio,
            channel_name=channel_name, summary=summary,
            published_at=published_at,
        )
        print("✅ Опубликовано\n")

        vid = _extract_video_id(video_url)
        _save_summary(vid, expert_name, channel_name, summary,
                      video_title=display_title, video_published_at=published_at)
        _mark_expert_seen_today(expert_name)

        if EXTRACT_PREDICTIONS:
            try:
                print("📊 Прогнозы...")
                preds = extract_predictions(transcript, expert_name)
                if preds:
                    saved = _save_predictions(vid, expert_name, channel_name, preds)
                    print(f"✅ Сохранено {saved}")
                    for p in preds:
                        print(f"   • {p[:120]}")
                else:
                    print("   нет")
            except Exception as e:
                print(f"⚠️  прогнозы: {e}")

        return True, ""

    except Exception as e:
        err_type = type(e).__name__
        err_msg = str(e)[:500]
        print(f"\n❌ {err_type}: {err_msg}")
        traceback.print_exc()
        sys.stderr.flush()
        _log_memory("в except")
        if audio_file and os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except Exception:
                pass
        return False, f"{err_type}: {err_msg}"


# ╔══════════════════════════════════════════════════════════╗
# ║              ВОРКЕР                                       ║
# ╚══════════════════════════════════════════════════════════╝

async def _worker_loop():
    print("🔧 Воркер запущен")
    while _SHUTDOWN_EVENT is None or not _SHUTDOWN_EVENT.is_set():
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
                sys.stderr.flush()
                _handle_task_failure(tid, err_text, attempts_now)

            try:
                import gc
                gc.collect()
            except Exception:
                pass

        except Exception as e:
            print(f"⚠️  воркер: {e}")
            traceback.print_exc()
            sys.stderr.flush()
            await asyncio.sleep(WORKER_POLL_INTERVAL)

    print("🛑 Воркер завершён")


# ╔══════════════════════════════════════════════════════════╗
# ║              АВТОЦИКЛ                                     ║
# ╚══════════════════════════════════════════════════════════╝

async def check_all_channels():
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
        if _SHUTDOWN_EVENT is not None and _SHUTDOWN_EVENT.is_set():
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


async def _heartbeat_job():
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


# ╔══════════════════════════════════════════════════════════╗
# ║              TELEGRAM HANDLERS                            ║
# ╚══════════════════════════════════════════════════════════╝

def _is_owner(message: Message) -> bool:
    return message.from_user and message.from_user.id == OWNER_USER_ID


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    if not _is_owner(message):
        return
    mode = "🔧 ДИАГНОСТИКА" if DIAGNOSTIC_MODE else "📦 ОБЫЧНЫЙ"
    await message.answer(
        f"<b>ГЕОПОЛИТРУК</b>\n"
        f"Режим: {mode}\n"
        f"Модель: {GROQ_MODEL}\n\n"
        "<b>Команды:</b>\n"
        "/queue — очередь\n"
        "/stats — статистика\n"
        "/quota — квота YouTube\n"
        "/errors — ошибки\n"
        "/predictions — прогнозы\n"
        "/reset — сброс очереди\n"
        "/cancel &lt;id&gt; — отмена\n"
        "/priority — рейтинг\n"
        "/experts_today — кто выступал"
    )


@dp.message(Command("queue"))
async def cmd_queue(message: Message):
    if not _is_owner(message):
        return
    tasks = _queue_list(20)
    if not tasks:
        await message.answer("📭 Очередь пуста")
        return
    lines = ["<b>📋 Очередь:</b>\n"]
    for t in tasks:
        ic = "🔄" if t['status'] == 'processing' else "⏳"
        src = "👤" if t['source'] == 'telegram' else "🤖"
        retry = ""
        if t.get('next_retry_at'):
            retry = f" [retry {t['next_retry_at']}]"
        lines.append(f"{ic}{src} #{t['id']} [P{t['priority']}] "
                     f"(попыток: {t['attempts']}){retry} "
                     f"{(t['video_title'] or t['video_url'])[:50]}")
    await message.answer("\n".join(lines))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not _is_owner(message):
        return
    s = _queue_stats()
    q = _quota_info()
    mode = "🔧" if DIAGNOSTIC_MODE else "📦"
    text = (f"<b>📊 Статистика</b> ({mode})\n\n"
            f"⏳ Pending: {s.get('pending', 0)}\n"
            f"🔄 Processing: {s.get('processing', 0)}\n"
            f"✅ Done: {s.get('done', 0)}\n"
            f"❌ Error: {s.get('error', 0)}\n\n"
            f"📊 Квота YouTube: {q['count']}/{YOUTUBE_DAILY_QUOTA}")
    if PSUTIL_AVAILABLE:
        try:
            mem_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            text += f"\n💾 RAM: {mem_mb:.0f} MB"
        except Exception:
            pass
    await message.answer(text)


@dp.message(Command("quota"))
async def cmd_quota(message: Message):
    if not _is_owner(message):
        return
    q = _quota_info()
    ops = "\n".join(f"  • {op}: {n}" for op, n in q["by_op"].items()) or "  (нет)"
    remaining = YOUTUBE_DAILY_QUOTA - q["count"]
    pct = round(q["count"] / YOUTUBE_DAILY_QUOTA * 100, 2)
    await message.answer(
        f"<b>📊 Квота YouTube</b>\n\n"
        f"Дата (UTC): {q['date']}\n"
        f"Использовано: <b>{q['count']}</b> / {YOUTUBE_DAILY_QUOTA} ({pct}%)\n"
        f"Осталось: {remaining}\n\n"
        f"<b>По операциям:</b>\n{ops}"
    )


@dp.message(Command("errors"))
async def cmd_errors(message: Message):
    if not _is_owner(message):
        return
    errors = _recent_errors(10)
    if not errors:
        await message.answer("✅ Ошибок нет")
        return
    lines = ["<b>❌ Последние ошибки:</b>\n"]
    for e in errors:
        title = (e['video_title'] or e['video_url'])[:50]
        msg = (e['error_message'] or "—")[:400]
        lines.append(f"<b>#{e['id']}</b> (попыток: {e['attempts']})\n"
                     f"  {title}\n"
                     f"  <code>{html.escape(msg)}</code>\n")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "…"
    await message.answer(text)


@dp.message(Command("predictions"))
async def cmd_predictions(message: Message):
    if not _is_owner(message):
        return
    preds = _recent_predictions(15)
    if not preds:
        await message.answer("📭 Прогнозов пока нет")
        return
    lines = ["<b>🔮 Последние прогнозы:</b>\n"]
    for p in preds:
        icon = {"confirmed": "✅", "refuted": "❌",
                "unverifiable": "❔"}.get(p.get("verdict"), "⏳")
        lines.append(f"{icon} <b>#{p['id']}</b> — {p['expert_name']}\n"
                     f"  {(p['prediction_text'] or '')[:200]}\n")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "…"
    await message.answer(text)


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if not _is_owner(message):
        return
    n = _reset_queue()
    await message.answer(f"🛑 Сброшено в error: <b>{n}</b> задач")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not _is_owner(message):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /cancel <id>")
        return
    try:
        tid = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    ok, msg = _cancel_task(tid)
    await message.answer(("✅ " if ok else "❌ ") + f"#{tid}: {msg}")


@dp.message(Command("priority"))
async def cmd_priority(message: Message):
    if not _is_owner(message):
        return
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT expert_name, "
        "SUM(CASE WHEN verdict='confirmed' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN verdict='refuted' THEN 1 ELSE 0 END) "
        "FROM predictions WHERE verdict IN ('confirmed','refuted') "
        "GROUP BY expert_name"
    ).fetchall()
    conn.close()
    if not rows:
        await message.answer("Пока нет проверенных прогнозов")
        return
    rated = []
    for exp, c, r in rows:
        c = c or 0
        r = r or 0
        if c + r > 0:
            rated.append((exp, c, r, round(c / (c + r) * 100, 1)))
    rated.sort(key=lambda x: -x[3])
    lines = ["<b>🏆 Рейтинг:</b>\n"]
    for i, (e, c, r, acc) in enumerate(rated[:15], 1):
        lines.append(f"{i}. {e}: {acc}% (✅{c}/❌{r})")
    await message.answer("\n".join(lines))


@dp.message(Command("experts_today"))
async def cmd_experts_today(message: Message):
    if not _is_owner(message):
        return
    exp = _get_experts_seen_today()
    if not exp:
        await message.answer("Сегодня никто не выступал")
        return
    lines = [f"<b>👥 Сегодня ({_get_today_msk()}):</b>\n"]
    for e in sorted(exp):
        lines.append(f"• {e} — {get_expert_priority_rating(e)}%")
    await message.answer("\n".join(lines))


@dp.message(F.text)
async def on_text(message: Message):
    if not _is_owner(message):
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    if not _is_youtube_url(text):
        return

    url = _extract_youtube_url(text)
    vid = _extract_video_id(url)

    if vid and _is_video_processed(vid):
        await message.answer("⚠️ Уже обработано ранее")
        return

    tid = _add_task_to_queue(video_url=url, video_id=vid, priority=10, source="telegram")
    if tid:
        await message.answer(
            f"✅ Добавлено: <b>#{tid}</b>\n"
            f"Приоритет: 10 (ручная)\n"
            f"Обработается после текущей задачи"
        )
    else:
        await message.answer("⏭ Уже в очереди")


# ╔══════════════════════════════════════════════════════════╗
# ║                       ЗАПУСК                             ║
# ╚══════════════════════════════════════════════════════════╝

async def _main():
    global _SHUTDOWN_EVENT
    _SHUTDOWN_EVENT = asyncio.Event()

    print("🔍 [1/10] _main() стартовал")
    print(f"🔍 sys.argv = {sys.argv}")
    print(f"🔍 CWD = {os.getcwd()}")

    def _handle_shutdown_signal():
        print("\n⏹ Получен сигнал завершения")
        _SHUTDOWN_EVENT.set()

    try:
        loop = asyncio.get_running_loop()
        print(f"🔍 [2/10] Running loop: {type(loop).__name__}")
    except Exception as e:
        print(f"⚠️  get_running_loop: {e}")

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_shutdown_signal)
                print(f"🔍 signal {sig.name} зарегистрирован")
            except (NotImplementedError, RuntimeError, ValueError) as _e:
                print(f"🔍 signal {sig.name} не зарегистрирован ({type(_e).__name__})")
    except Exception as e:
        print(f"⚠️  signal setup: {e}")

    print("🔍 [3/10] CLI checks")
    if "--predictions" in sys.argv:
        _init_db()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM predictions ORDER BY prediction_date DESC LIMIT 50"
        ).fetchall()
        conn.close()
        if not rows:
            print("Нет прогнозов")
            return
        for r in rows:
            m = {"confirmed": "✅", "refuted": "❌", "unverifiable": "❔"}.get(r["verdict"], "⏳")
            print(f"{m} [{r['id']}] {r['expert_name']}: {r['prediction_text'][:100]}")
        return

    if "--mark" in sys.argv:
        _init_db()
        idx = sys.argv.index("--mark")
        try:
            pid = int(sys.argv[idx + 1])
            v = sys.argv[idx + 2]
        except (IndexError, ValueError):
            print("python bot.py --mark <id> confirmed|refuted|unverifiable")
            return
        if v not in ("confirmed", "refuted", "unverifiable"):
            print("❌ Неверный вердикт")
            return
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE predictions SET verdict = ?, verdict_date = CURRENT_TIMESTAMP WHERE id = ?",
            (v, pid)
        )
        conn.commit()
        conn.close()
        print(f"✅ [{pid}] → {v}")
        return

    if "--queue" in sys.argv:
        _init_db()
        for t in _queue_list():
            ic = "🔄" if t['status'] == 'processing' else "⏳"
            src = "👤" if t['source'] == 'telegram' else "🤖"
            print(f"{ic}{src} #{t['id']} [P{t['priority']}] "
                  f"(попыток: {t['attempts']}) "
                  f"{(t['video_title'] or t['video_url'])[:60]}")
        return

    if "--errors" in sys.argv:
        _init_db()
        errs = _recent_errors(20)
        if not errs:
            print("✅ Ошибок нет")
            return
        for e in errs:
            print(f"❌ #{e['id']} (попыток: {e['attempts']}): "
                  f"{(e['video_title'] or e['video_url'])[:50]}")
            print(f"   {e['error_message'] or '—'}")
        return

    if "--cancel" in sys.argv:
        _init_db()
        idx = sys.argv.index("--cancel")
        try:
            tid = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("python bot.py --cancel <id>")
            return
        ok, msg = _cancel_task(tid)
        print(("✅" if ok else "❌") + f" {msg}")
        return

    if "--reset" in sys.argv:
        _init_db()
        n = _reset_queue()
        print(f"🛑 Сброшено в error: {n}")
        return

    if "--process" in sys.argv:
        _init_db()
        idx = sys.argv.index("--process")
        try:
            url = sys.argv[idx + 1]
        except IndexError:
            print("python bot.py --process <url>")
            return
        info = get_video_info_ytdlp(url)
        ok, reason = await process_and_publish(
            url,
            video_title=info.get("title", ""),
            video_description=info.get("description", ""),
            video_duration=info.get("duration", 0),
            video_published_at=info.get("published_at", ""),
            channel_name=info.get("channel", ""),
            channel_type="media",
            skip_duration_check=True,
        )
        print(f"\n{'=' * 60}")
        print(f"Результат: {'✅ OK' if ok else '❌ ' + reason}")
        print(f"{'=' * 60}")
        await bot.session.close()
        return

    print("🔍 [4/10] перед _init_db")
    _cleanup_temp_files()
    _init_db()
    print("🔍 [5/10] после _init_db")

    if TEST_VIDEO_URL:
        print("\n🧪 ТЕСТ")
        ok, reason = await process_and_publish(TEST_VIDEO_URL, video_title=TEST_VIDEO_TITLE)
        print(f"Тест: {'✅ OK' if ok else '❌ ' + reason}")

    print("🔍 [6/10] перед check_all_channels")
    await check_all_channels()
    print("🔍 [7/10] после check_all_channels")

    print("🔍 [8/10] создаю воркер и планировщик")
    worker_task = asyncio.create_task(_worker_loop())

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_all_channels, "interval", hours=CHECK_INTERVAL_HOURS)

    if HEARTBEAT_HOURS > 0:
        scheduler.add_job(_heartbeat_job, "interval", hours=HEARTBEAT_HOURS)

    scheduler.start()
    print("🔍 [9/10] scheduler запущен")

    print(f"\n{'=' * 60}")
    print("🚀 БОТ ГОТОВ")
    print(f"   Воркер: запущен")
    print(f"   Планировщик: каждые {CHECK_INTERVAL_HOURS} ч")
    print(f"   Глубина: {HOURS_BACK} ч")
    print(f"   Whisper: {WHISPER_MODE} ({GROQ_WHISPER_MODEL})")
    print(f"   Модель LLM: {GROQ_MODEL}")
    print(f"   Owner: {OWNER_USER_ID}")
    print(f"   БД: {DB_PATH}")
    print(f"{'=' * 60}\n")

    print("🔍 [10/10] запускаю dp.start_polling()")
    try:
        await dp.start_polling(bot)
    finally:
        print("\n⏹ Останавливаюсь...")
        _SHUTDOWN_EVENT.set()
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        print("⏳ Ожидание завершения текущей задачи (до 10 мин)...")
        try:
            await asyncio.wait_for(worker_task, timeout=600)
            print("✅ Воркер завершён")
        except (asyncio.TimeoutError, asyncio.CancelledError):
            print("⏱ Таймаут — отменяю воркер")
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception as e:
            print(f"⚠️  Ошибка при остановке: {e}")
        try:
            await bot.session.close()
        except Exception:
            pass
        print("⏹ Завершено")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n\n⏹ Остановлено (Ctrl+C)")
    except BaseException as e:
        print(f"\n\n❌ КРИТИЧЕСКАЯ ОШИБКА: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.stderr.flush()