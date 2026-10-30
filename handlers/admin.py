"""Административные команды владельца."""

import html
import os
import sqlite3

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import (
    DB_PATH, DIAGNOSTIC_MODE, GROQ_MODEL,
    YOUTUBE_DAILY_QUOTA,
)
from db.queue import (
    _queue_stats, _queue_list, _recent_errors,
    _reset_queue, _cancel_task,
)
from db.predictions import get_expert_priority_rating
from db.experts import _get_today_msk, _get_experts_seen_today
from services.youtube import _quota_info

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


router = Router()

_OWNER_USER_ID = None


def set_owner_user_id(owner_id: int):
    """Вызывается из bot.py при старте, чтобы не импортировать bot сюда."""
    global _OWNER_USER_ID
    _OWNER_USER_ID = owner_id


def _is_owner(message: Message) -> bool:
    return message.from_user and message.from_user.id == _OWNER_USER_ID


def _ram_suffix() -> str:
    if not PSUTIL_AVAILABLE:
        return ""
    try:
        mem_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        return f"\n💾 RAM: {mem_mb:.0f} MB"
    except Exception:
        return ""


@router.message(Command("start", "help"))
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
        "/experts_today — кто выступал\n"
        "/cleanup_junk_predictions — удалить пустышки"
    )


@router.message(Command("queue"))
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


@router.message(Command("stats"))
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
            f"📊 Квота YouTube: {q['count']}/{YOUTUBE_DAILY_QUOTA}"
            f"{_ram_suffix()}")
    await message.answer(text)


@router.message(Command("quota"))
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


@router.message(Command("errors"))