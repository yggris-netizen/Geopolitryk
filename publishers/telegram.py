"""Публикация выжимки в Telegram-канал + пайплайн обработки видео."""

import asyncio
import os
import re
import sys
import traceback

from aiogram.enums import ParseMode

from config import (
    CHANNEL_ID, DIAGNOSTIC_MODE, EXTRACT_PREDICTIONS,
    GENERATE_HASHTAGS, MAX_VIDEO_DURATION_SECONDS,
    MIN_TRANSCRIPT_CHARS, MIN_VIDEO_DURATION_SECONDS,
    NEUTRALIZE_TITLE, TELEGRAM_MAX_LEN,
)
from db.experts import _mark_expert_seen_today
from db.predictions import _save_predictions
from db.summaries import _save_summary
from services.expert_detection import detect_expert_name, generate_expert_bio
from services.predictions_extraction import extract_predictions
from services.summarization import (
    generate_hashtags, neutralize_title, summarize_text,
    _check_context_errors, _format_title,
)
from services.transcription import download_audio, transcribe_audio
from services.youtube import (
    get_video_info_ytdlp, _extract_video_id, _get_thumbnail_url,
    _format_duration, _format_published_date,
)


_bot = None


def set_bot(bot_instance):
    """Вызывается из bot.py при старте."""
    global _bot
    _bot = bot_instance


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
            await _bot.send_photo(chat_id=CHANNEL_ID, photo=url,
                                  caption=caption, parse_mode=ParseMode.HTML)
            print("✅ Фото")
            sent = True
        except Exception as e:
            print(f"⚠️  Фото: {e}")
    if not sent:
        await _bot.send_message(chat_id=CHANNEL_ID, text=caption)

    hashtags = generate_hashtags(expert_name, summary) if GENERATE_HASHTAGS else ""
    text = f"{summary}\n\n🔗 <a href='{video_url}'>Смотреть оригинал</a>\n"
    if hashtags:
        text += f"\n{hashtags}\n"
    text += f"\n<i>Сгенерировано автоматически.</i>"

    if len(text) <= TELEGRAM_MAX_LEN:
        await _bot.send_message(chat_id=CHANNEL_ID, text=text)
        print("✅ Выжимка")
    else:
        print(f"✂️  Текст {len(text)} > {TELEGRAM_MAX_LEN}, разбиваю...")
        chunks = []
        current = ""
        for paragraph in text.split("\n\n"):
            if len(current) + len(paragraph) + 2 <= TELEGRAM_MAX_LEN:
                current = (current + "\n\n" + paragraph).strip() if current else paragraph
            else:
                if current:
                    chunks.append(current)
                if len(paragraph) > TELEGRAM_MAX_LEN:
                    sentences = re.split(r'(?<=[.!?])\s+', paragraph)
                    buf = ""
                    for s in sentences:
                        if len(buf) + len(s) + 1 <= TELEGRAM_MAX_LEN:
                            buf = (buf + " " + s).strip() if buf else s
                        else:
                            if buf:
                                chunks.append(buf)
                            buf = s[:TELEGRAM_MAX_LEN]
                    if buf:
                        chunks.append(buf)
                else:
                    current = paragraph
        if current:
            chunks.append(current)

        for i, part in enumerate(chunks, 1):
            header = f"<i>(часть {i}/{len(chunks)})</i>\n\n" if len(chunks) > 1 else ""
            await _bot.send_message(chat_id=CHANNEL_ID, text=header + part)
            print(f"✅ Выжимка {i}/{len(chunks)}")
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
        audio_file = await asyncio.to_thread(download_audio, video_url)
        print("✅ Скачано\n")

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
        try:
            sys.stderr.flush()
        except Exception:
            pass
        if audio_file and os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except Exception:
                pass
        return False, f"{err_type}: {err_msg}"