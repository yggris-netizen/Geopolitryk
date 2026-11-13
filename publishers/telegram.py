"""Публикация выжимки в Telegram + пайплайн обработки видео.

NLI-проверка: публикуются ТОЛЬКО verified тезисы (entailment).
Неподтверждённые (not_found / contradiction / neutral) — убираются.
Если verified пусто — не публиковать (no_verified_claims) + уведомление владельцу.

pending_voices сохраняется ВСЕГДА (даже при NOTIFY_UNKNOWN_VOICES=False),
уведомление владельцу о незнакомцах — по флагу NOTIFY_UNKNOWN_VOICES.

P1: neutralize_title ОТКЛЮЧЁН — заголовок публикуется как есть (КАПС, эмодзи, шутки).
"""

import asyncio
import json
import os
import re
import sys
import time
import traceback

from aiogram.enums import ParseMode
from aiogram.types import LinkPreviewOptions

from config import QUOTE_PREVIEW_LEN, ERROR_PREVIEW_LEN, COMMENTARY_MIN_CHARS
from config import (
    CHANNEL_ID, DIAGNOSTIC_MODE, EXTRACT_PREDICTIONS,
    GENERATE_HASHTAGS, GROQ_PREDICTIONS_PAUSE,
    MAX_VIDEO_DURATION_SECONDS,
    MIN_TRANSCRIPT_CHARS, MIN_VIDEO_DURATION_SECONDS,
    NEUTRALIZE_TITLE, NOTIFY_UNKNOWN_VOICES, OWNER_USER_ID, TELEGRAM_MAX_LEN,
)
from db.experts import _mark_expert_seen_today
from db.pending_voices import save_pending
from db.predictions import _save_predictions
from db.summaries import _save_summary
from db.transcripts import get_cached_transcript, save_cached_transcript
from db.transcripts_raw import (
    save_transcript_raw, get_transcript_raw_segments,
    get_transcript_raw_expert_name,
)
from handlers.voice_registration import build_unknown_notification
from services.expert_detection import detect_experts
from services.predictions_extraction import (
    extract_predictions, extract_predictions_from_theses,
)
from services.commentary import (
    generate_commentary, generate_commentary_with_theses,
)
from services.summarization import (
    generate_hashtags,
    _format_title,
)
from services.transcription import (
    CookiesExpiredError, download_audio, transcribe_audio,
)
from services.voice_identification import (
    assign_speakers_to_segments, build_speaker_transcript, identify_speakers,
)
from services.youtube import (
    get_video_info_ytdlp, _extract_video_id, _get_thumbnail_url,
    _format_duration, _format_published_date,
)


_bot = None

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_LAST_COOKIES_ALERT_TS = 0.0
_COOKIES_ALERT_INTERVAL_SEC = 3600


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


def _unique_names(experts: list[dict]) -> list[str]:
    seen = set()
    result = []
    for e in experts:
        name = (e.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _speakers_block(experts: list[dict]) -> str:
    """Гости (СПИКЕР + специализация) + ведущие (ВЕДУЩИЙ)."""
    if not experts:
        return "<b>СПИКЕРЫ</b>\nне определены"

    parts = []

    # 1. Гости (role=guest) со специализацией
    guests = [e for e in experts if e.get("role") == "guest"]
    if guests:
        parts.append("<b>СПИКЕР</b>")
        for g in guests:
            spec = (g.get("specialization") or "").strip()
            line = g["name"]
            if spec:
                line += f" — {spec}"
            parts.append(line)

    # 2. Ведущие (role=author) — без специализации
    hosts = [e for e in experts if e.get("role") == "author"]
    if hosts:
        parts.append("<b>ВЕДУЩИЙ</b>")
        parts.append(", ".join(h["name"] for h in hosts))
    elif not guests:
        # fallback: если role нет — все как гости
        parts.append("<b>СПИКЕР</b>")
        for e in experts:
            spec = (e.get("specialization") or "").strip()
            line = e["name"]
            if spec:
                line += f" — {spec}"
            parts.append(line)

    return "\n\n".join(parts)


def _experts_as_string(experts: list[dict]) -> str:
    names = _unique_names(experts)
    return ", ".join(names) if names else "Неизвестный спикер"


def _format_verified_summary(verified: list) -> str:
    """Формирует текст из verified тезисов с таймкодами и цитатами."""
    if not verified:
        return ""
    lines = []
    for v in verified:
        ts = _format_duration(v.get("start", 0))
        claim = (v.get("claim") or "").strip()
        quote = (v.get("quote") or "").strip()
        if not claim:
            continue
        line = f"• <b>[{ts}]</b> {claim}"
        if quote:
            q = quote[:QUOTE_PREVIEW_LEN] + ("…" if len(quote) > QUOTE_PREVIEW_LEN else "")
            line += f"\n<i>«{q}»</i>"
        lines.append(line)
    return "\n\n".join(lines)


async def _notify_owner_text(text: str) -> None:
    if not _bot:
        return
    try:
        await _bot.send_message(
            chat_id=OWNER_USER_ID, text=text,
            link_preview_options=NO_PREVIEW,
        )
    except Exception as e:
        print(f"[WARN]  _notify_owner_text: {e}")


async def _notify_cookies_expired(video_url: str, error_text: str) -> None:
    global _LAST_COOKIES_ALERT_TS
    now = time.time()
    if now - _LAST_COOKIES_ALERT_TS < _COOKIES_ALERT_INTERVAL_SEC:
        print(f"   [SKIP] cookies-алерт пропущен (был недавно)")
        return
    _LAST_COOKIES_ALERT_TS = now
    text = (
        f"[COOKIE] <b>Cookies протухли</b>\n\n"
        f"Скачивание не работает: <code>{error_text[:ERROR_PREVIEW_LEN]}</code>\n\n"
        f"[LINK] {video_url}\n\n"
        f"Обновите <code>cookies.txt</code> (Firefox → Get cookies.txt LOCALLY) "
        f"и положите рядом с <code>bot.py</code>."
    )
    await _notify_owner_text(text)


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
            print("[OK] Фото")
            sent = True
        except Exception as e:
            print(f"[WARN]  Фото: {e}")
    if not sent:
        await _bot.send_message(
            chat_id=CHANNEL_ID, text=caption,
            link_preview_options=NO_PREVIEW,
        )

    hashtags = generate_hashtags(_experts_as_string(experts), summary) if GENERATE_HASHTAGS else ""
    text = f"{summary}\n\n[LINK] <a href='{video_url}'>Смотреть оригинал</a>\n"
    if hashtags:
        text += f"\n{hashtags}\n"
    text += f"\n<i>Сгенерировано автоматически.</i>"

    if len(text) <= TELEGRAM_MAX_LEN:
        await _bot.send_message(
            chat_id=CHANNEL_ID, text=text,
            link_preview_options=NO_PREVIEW,
        )
        print("[OK] Выжимка (без превью ссылки)")
    else:
        print(f"[CUT]  Текст {len(text)} > {TELEGRAM_MAX_LEN}, разбиваю...")
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
            print(f"[OK] Выжимка {i}/{len(chunks)} (без превью ссылки)")
            await asyncio.sleep(1)


async def notify_owner_about_unknown(video_url, video_title, channel_name,
                                      unknown: list, video_id: str,
                                      experts: list = None):
    """Сохраняет pending ВСЕГДА. Уведомление — только если NOTIFY_UNKNOWN_VOICES."""
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

    print(f"[SAVE] pending: сохранено {len(pending_list)} незнакомцев")

    if not NOTIFY_UNKNOWN_VOICES:
        print(f"   [SKIP] Уведомление отключено (NOTIFY_UNKNOWN_VOICES=False)")
        return

    print(f"[UL] Уведомление владельцу о {len(pending_list)} незнакомых голосах...")
    text, keyboard = build_unknown_notification(
        video_url, video_title, pending_list,
        known_experts=experts or [],
    )
    try:
        await _bot.send_message(
            chat_id=OWNER_USER_ID, text=text,
            reply_markup=keyboard,
            link_preview_options=NO_PREVIEW,
        )
    except Exception as e:
        print(f"[WARN]  notify_owner: {e}")


async def process_and_publish(video_url, video_title="", video_description="",
                               video_duration=0, video_published_at="",
                               channel_name="", channel_type="",
                               channel_owner="", skip_duration_check=False,
                               audio_file_override=None) -> tuple:
    print(f"\n{'=' * 60}")
    print(f"ОБРАБОТКА: {video_url}")
    if video_title:
        print(f"  Заголовок: {video_title[:80]}")
    if channel_name:
        print(f"  Канал: {channel_name}")
    print(f"{'=' * 60}\n")

    audio_file = audio_file_override
    audio_from_caller = audio_file_override is not None
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
            print("[SEARCH] yt-dlp (полный заголовок/метаданные)...")
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
        print(f"[INFO] description: {len(video_description or '')} символов")

        transcript_text = ""
        if vid:
            transcript_text = get_cached_transcript(vid)
            if transcript_text:
                print(f"[SAVE] транскрипт из кеша ({len(transcript_text)} символов)")

        transcript_segments = []
        clusters = []

        if not transcript_text:
            if not audio_from_caller:
                print("[DL] Шаг 1/4: Скачивание...")
                try:
                    audio_file = await asyncio.to_thread(download_audio, video_url)
                except CookiesExpiredError as e:
                    print(f"   [COOKIE] CookiesExpiredError: {e}")
                    await _notify_cookies_expired(video_url, str(e))
                    return False, "cookies_expired"
                print("[OK] Скачано\n")
            else:
                print(f"[DL] аудио передано извне: {audio_file}\n")

            print("[MIC] Шаг 2/4: Транскрибация...")
            transcript_text, transcript_segments = await asyncio.to_thread(
                transcribe_audio, audio_file, duration
            )
            print(f"[STAT] Длина: {len(transcript_text)}, сегментов: {len(transcript_segments)}")

            if audio_file and os.path.exists(audio_file):
                print("[MIC] Шаг 2.5: Voice identification...")
                clusters = await asyncio.to_thread(
                    identify_speakers, audio_file, transcript_segments
                )
                if clusters:
                    transcript_segments = assign_speakers_to_segments(transcript_segments, clusters)

            if audio_file and os.path.exists(audio_file) and not audio_from_caller:
                try:
                    os.remove(audio_file)
                    print("🧹 аудио удалено")
                except Exception as e:
                    print(f"[WARN]  cleanup audio: {e}")
            audio_file = None

            if vid and transcript_text:
                save_cached_transcript(vid, transcript_text)
                print(f"[SAVE] транскрипт сохранён в кеш")

        transcript_len = len(transcript_text.strip())
        print(f"[STAT] Длина транскрипта: {transcript_len}")

        if transcript_len < MIN_TRANSCRIPT_CHARS:
            return False, f"transcript_too_short: {transcript_len}"

        print(f"[OK] ({transcript_len})\n")

        if DIAGNOSTIC_MODE:
            print("[TOOL] ДИАГНОСТИКА: транскрибация прошла")
            return True, f"diagnostic_ok: {transcript_len}"

        # P1: neutralize_title ОТКЛЮЧЁН — заголовок как есть
        display_title = video_title
        # if NEUTRALIZE_TITLE and video_title:
        #     print("[STYLE] Нейтрализация...")
        #     n = neutralize_title(video_title)
        #     if n and n != video_title:
        #         display_title = n
        #         print(f"[OK] {display_title}\n")

        print("[SEARCH] Спикеры...")
        # 1) Приоритет: transcripts_raw (реальные имена с прошлого voice-прогона)
        experts = []
        unknown = []
        if vid:
            cached_experts = get_transcript_raw_expert_name(vid)
            if cached_experts:
                names = [n.strip() for n in cached_experts.split(",") if n.strip()]
                experts = [{
                    "name": n, "role": "guest", "specialization": "",
                    "speaker": "", "duration_sec": 0,
                } for n in names]
                print(f"   [OK] experts из transcripts_raw: {cached_experts}")
        # 2) Если пусто — detect_experts (voice + fallback description/channel)
        if not experts:
            experts, unknown = detect_experts(
                video_title, transcript_text,
                description=video_description,
                channel_name=channel_name,
                clusters=clusters,
                video_url=video_url,
            )
        for e in experts:
            spk = f" [{e.get('speaker', '')}]" if e.get("speaker") else ""
            print(f"   [OK] {e['name']}{spk}")
        print()

        if unknown:
            await notify_owner_about_unknown(
                video_url, display_title, channel_name, unknown, vid,
                experts=experts,
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
            print(f"[SAVE] transcripts_raw сохранён ({len(transcript_segments)} сегментов)\n")

        # Если транскрипт из кеша — сегменты могли не загрузиться
        if not transcript_segments and vid:
            transcript_segments = get_transcript_raw_segments(vid)
            if transcript_segments:
                print(f"[SAVE] сегменты из transcripts_raw: {len(transcript_segments)}\n")

        print("[LLM] Шаг 3/4: Комментарий...")
        commentary_text, all_theses = generate_commentary_with_theses(
            transcript_text, transcript_segments
        )
        if not commentary_text or len(commentary_text) < COMMENTARY_MIN_CHARS:
            msg = (
                f"[WARN] <b>Не публикую</b>: комментарий пуст или короткий\n\n"
                f"[LINK] {video_url}\n"
                f"[STAT] Длина: {len(commentary_text)} символов"
            )
            await _notify_owner_text(msg)
            print(f"[ERR] no_commentary: {len(commentary_text)} символов")
            return False, "no_commentary"

        summary_text = commentary_text
        print(f"[OK] Комментарий: {len(commentary_text)} символов\n")

        print("[UL] Шаг 4/4: Отправка...")
        await send_expert_post(
            video_url=video_url, video_title=display_title,
            experts=experts,
            channel_name=channel_name, summary=summary_text,
            published_at=published_at,
        )
        print("[OK] Опубликовано\n")

        experts_str = _experts_as_string(experts)
        main_speaker = ""
        if experts:
            main_speaker = experts[0].get("speaker", "") or ""

        _save_summary(vid, experts_str, channel_name, summary_text,
                      video_title=display_title, video_published_at=published_at)
        for e in experts:
            _mark_expert_seen_today(e["name"])

        if EXTRACT_PREDICTIONS:
            try:
                print(f"[PAUSE]  пауза {GROQ_PREDICTIONS_PAUSE}с перед прогнозами...")
                await asyncio.sleep(GROQ_PREDICTIONS_PAUSE)
                print("[STAT] Прогнозы...")
                if all_theses and len(all_theses) >= 5:
                    preds = extract_predictions_from_theses(all_theses, experts_str)
                else:
                    print(f"   [STAT] тезисов {len(all_theses) if all_theses else 0} < 5, fallback на extract_predictions")
                    preds = extract_predictions(transcript_text, experts_str)
                if preds:
                    saved = _save_predictions(
                        vid, experts_str, channel_name, preds,
                        speaker_id=main_speaker,
                    )
                    print(f"[OK] Сохранено {saved}")
                else:
                    print("   нет")
            except Exception as e:
                print(f"[WARN]  прогнозы: {e}")

        return True, ""

    except Exception as e:
        err_type = type(e).__name__
        err_msg = str(e)[:500]
        print(f"\n[ERR] {err_type}: {err_msg}")
        traceback.print_exc()
        if audio_file and os.path.exists(audio_file) and not audio_from_caller:
            try:
                os.remove(audio_file)
            except Exception as e:
                print(f"[WARN]  cleanup audio (error): {e}")
        return False, f"{err_type}: {err_msg}"