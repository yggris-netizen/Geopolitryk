"""Публикация выжимки в Telegram-канал + пайплайн обработки видео.

Voice fingerprinting:
1. Скачали аудио → транскрибация.
2. Voice identification → кластеры.
3. detect_experts → experts + unknown.
4. Незнакомые → pending_voices + уведомление с [✅ Спикер] [⏭ Пропустить].
5. Публикация: «Спикеры: A, B» + «КАНАЛ: <name>» (если есть).
"""

import asyncio
import os
import re
import sys
import traceback

from aiogram.enums import ParseMode
from aiogram.types import LinkPreviewOptions

from config import (
    CHANNEL_ID, DIAGNOSTIC_MODE, EXTRACT_PREDICTIONS,
    GENERATE_HASHTAGS, GROQ_PREDICTIONS_PAUSE,
    MAX_VIDEO_DURATION_SECONDS,
    MIN_TRANSCRIPT_CHARS, MIN_VIDEO_DURATION_SECONDS,
    NEUTRALIZE_TITLE, OWNER_USER_ID, TELEGRAM_MAX_LEN,
)
from db.experts import _mark_expert_seen_today
from db.pending_voices import save_pending
from db.predictions import _save_predictions
from db.summaries import _save_summary
from db.transcripts import get_cached_transcript, save_cached_transcript
from db.transcripts_raw import save_transcript_raw
from handlers.voice_registration import build_unknown_notification
from services.expert_detection import detect_experts
from services.predictions_extraction import extract_predictions
from services.summarization import (
    generate_hashtags, neutralize_title, summarize_text,
    _check_context_errors, _format_title,
)
from services.transcription import download_audio, transcribe_audio
from services.voice_identification import (
    assign_speakers_to_segments, build_speaker_transcript, identify_speakers,
)
from services.youtube import (
    get_video_info_ytdlp, _extract_video_id, _get_thumbnail_url,
    _format_duration, _format_published_date,
)


_bot = None

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def set_bot(bot_instance):
    global _bot
    _bot = bot_instance


def _looks_truncated_title(title: str) -> bool:
    if not title:
        return True
    if len(title) < 10:
        return True
    stripped = title.rstrip()
    if stripped.endswith(("/", "-", "—", "|", ":", ",")):
        return True
    if not re.search(r'[А-Яа-яA-Za-z]', title):
        return True
    return False


def _speakers_block(experts: list[dict]) -> str:
    """Формат: «Спикеры: A, B» или «Спикер: A». Если пусто — «Спикеры: не определены»."""
    if not experts:
        return "<b>СПИКЕРЫ</b>\nне определены"
    names = [e["name"] for e in experts if e.get("name")]
    if not names:
        return "<b>СПИКЕРЫ</b>\nне определены"
    if len(names) == 1:
        return f"<b>СПИКЕР</b>\n{names[0]}"
    return f"<b>СПИКЕРЫ</b>\n{', '.join(names)}"


def _experts_as_string(experts: list[dict]) -> str:
    if not experts:
        return "Неизвестный спикер"
    return ", ".join(e["name"] for e in experts if e.get("name")) or "Неизвестный спикер"


async def send_expert_post(video_url, video_title, experts, channel_name, summary,
                            published_at=""):
    video_id = _extract_video_id(video_url)
    date_str = _format_published_date(published_at)

    parts = []
    if video_title:
        parts.append(_format_title(video_title))
    parts.append(_speakers_block(experts))

    if channel_name:
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
        await _bot.send_message(
            chat_id=CHANNEL_ID, text=caption,
            link_preview_options=NO_PREVIEW,
        )

    hashtags = generate_hashtags(_experts_as_string(experts), summary) if GENERATE_HASHTAGS else ""
    text = f"{summary}\n\n🔗 <a href='{video_url}'>Смотреть оригинал</a>\n"
    if hashtags:
        text += f"\n{hashtags}\n"
    text += f"\n<i>Сгенерировано автоматически.</i>"

    if len(text) <= TELEGRAM_MAX_LEN:
        await _bot.send_message(
            chat_id=CHANNEL_ID, text=text,
            link_preview_options=NO_PREVIEW,
        )
        print("✅ Выжимка (без превью ссылки)")
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
            await _bot.send_message(
                chat_id=CHANNEL_ID, text=header + part,
                link_preview_options=NO_PREVIEW,
            )
            print(f"✅ Выжимка {i}/{len(chunks)} (без превью ссылки)")
            await asyncio.sleep(1)


async def notify_owner_about_unknown(video_url, video_title, channel_name,
                                      unknown: list, video_id: str):
    """Уведомляет владельца о незнакомых голосах с inline-кнопками."""
    if not unknown:
        return
    if not _bot:
        return

    pending_list = []
    for u in unknown:
        pid = save_pending(
            video_id=video_id,
            video_url=video_url,
            embedding=u["embedding"],
            duration_sec=u["duration_sec"],
            channel_name=channel_name,
            speaker_id=u.get("speaker_id", ""),
        )
        if pid:
            pending_list.append({
                "id": pid,
                "speaker_id": u.get("speaker_id", ""),
                "duration_sec": u["duration_sec"],
            })

    if not pending_list:
        return

    print(f"📤 Уведомление владельцу о {len(pending_list)} незнакомых голосах...")
    text, keyboard = build_unknown_notification(video_url, video_title, pending_list)
    try:
        await _bot.send_message(
            chat_id=OWNER_USER_ID, text=text,
            reply_markup=keyboard,
            link_preview_options=NO_PREVIEW,
        )
    except Exception as e:
        print(f"⚠️  notify_owner: {e}")


async def process_and_publish(video_url, video_title="", video_description="",
                               video_duration=0, video_published_at="",
                               channel_name="", channel_type="",
                               channel_owner="", skip_duration_check=False) -> tuple:
    print(f"\n{'=' * 60}")
    print(f"ОБРАБОТКА: {video_url}")
    if video_title:
        print(f"  Заголовок: {video_title[:80]}")
    if channel_name:
        print(f"  Канал: {channel_name}")
    print(f"{'=' * 60}\n")

    audio_file = None
    try:
        duration = video_duration
        published_at = video_published_at
        vid = _extract_video_id(video_url)

        need_info = (
            not video_title
            or _looks_truncated_title(video_title)
            or (not video_description and not channel_name)
        )
        if need_info:
            print("🔍 yt-dlp (полный заголовок/метаданные)...")
            info = await asyncio.to_thread(get_video_info_ytdlp, video_url)
            full_title = info.get("title", "")
            if full_title and len(full_title) > len(video_title or ""):
                video_title = full_title
            if not video_description:
                video_description = info.get("description", "")
            if not duration:
                duration = info.get("duration", 0)
            if not published_at:
                published_at = info.get("published_at", "")
            if not channel_name:
                channel_name = info.get("channel", "")

            if not skip_duration_check and duration > 0:
                if duration < MIN_VIDEO_DURATION_SECONDS:
                    return False, f"too_short: {duration}s"
                if duration > MAX_VIDEO_DURATION_SECONDS:
                    return False, f"too_long: {duration}s"

        if not published_at:
            extra = await asyncio.to_thread(get_video_info_ytdlp, video_url)
            published_at = extra.get("published_at", "")

        transcript_text = ""
        if vid:
            transcript_text = get_cached_transcript(vid)
            if transcript_text:
                print(f"💾 транскрипт из кеша ({len(transcript_text)} символов)")

        transcript_segments = []
        clusters = []

        if not transcript_text:
            print("📥 Шаг 1/4: Скачивание...")
            audio_file = await asyncio.to_thread(download_audio, video_url)
            print("✅ Скачано\n")

            print("🎤 Шаг 2/4: Транскрибация...")
            transcript_text, transcript_segments = await asyncio.to_thread(
                transcribe_audio, audio_file, duration
            )
            print(f"📊 Длина: {len(transcript_text)}, сегментов: {len(transcript_segments)}")

            if audio_file and os.path.exists(audio_file):
                print("🎤 Шаг 2.5: Voice identification...")
                clusters = await asyncio.to_thread(
                    identify_speakers, audio_file, transcript_segments
                )
                if clusters:
                    transcript_segments = assign_speakers_to_segments(transcript_segments, clusters)

            if audio_file and os.path.exists(audio_file):
                try:
                    os.remove(audio_file)
                    print("🧹 аудио удалено")
                except Exception:
                    pass
            audio_file = None

            if vid and transcript_text:
                save_cached_transcript(vid, transcript_text)
                print(f"💾 транскрипт сохранён в кеш")

        transcript_len = len(transcript_text.strip())
        print(f"📊 Длина транскрипта: {transcript_len}")

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

        print("🔍 Спикеры...")
        experts, unknown = detect_experts(
            video_title, transcript_text,
            channel_name=channel_name,
            clusters=clusters,
            video_url=video_url,
        )
        for e in experts:
            spk = f" [{e.get('speaker', '')}]" if e.get("speaker") else ""
            print(f"   ✅ {e['name']}{spk}")
        print()

        if unknown:
            await notify_owner_about_unknown(
                video_url, display_title, channel_name, unknown, vid
            )

        if vid and transcript_segments:
            save_transcript_raw(
                video_id=vid,
                interview_id=None,
                expert_name=_experts_as_string(experts),
                source_url=video_url,
                segments=transcript_segments,
                language="ru",
                duration_sec=int(duration or 0),
                schema_version=2,
            )
            print(f"💾 transcripts_raw сохранён ({len(transcript_segments)} сегментов)\n")

        print("🧠 Шаг 3/4: Суммаризация...")
        summary = summarize_text(transcript_text)
        summary = _check_context_errors(summary, transcript_text)
        print("✅ Готово\n")

        print("📤 Шаг 4/4: Отправка...")
        await send_expert_post(
            video_url=video_url, video_title=display_title,
            experts=experts,
            channel_name=channel_name, summary=summary,
            published_at=published_at,
        )
        print("✅ Опубликовано\n")

        experts_str = _experts_as_string(experts)
        _save_summary(vid, experts_str, channel_name, summary,
                      video_title=display_title, video_published_at=published_at)
        for e in experts:
            _mark_expert_seen_today(e["name"])

        if EXTRACT_PREDICTIONS:
            try:
                print(f"⏸  пауза {GROQ_PREDICTIONS_PAUSE}с перед прогнозами...")
                await asyncio.sleep(GROQ_PREDICTIONS_PAUSE)
                print("📊 Прогнозы...")
                preds = extract_predictions(transcript_text, experts_str)
                if preds:
                    saved = _save_predictions(vid, experts_str, channel_name, preds)
                    print(f"✅ Сохранено {saved}")
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
        if audio_file and os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except Exception:
                pass
        return False, f"{err_type}: {err_msg}"