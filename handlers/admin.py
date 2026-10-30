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
from db.predictions import (
    _recent_predictions, get_expert_priority_rating,
)
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


@router.message(Command("predictions"))
async def cmd_predictions(message: Message):
    if not _is_owner(message):
        return
    # Делегируем в handlers/predictions.py, если он подключён
    try:
        from handlers.predictions import cmd_predictions as _pred_list
        await _pred_list(message)
    except ImportError:
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


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    if not _is_owner(message):
        return
    n = _reset_queue()
    await message.answer(f"🛑 Сброшено в error: <b>{n}</b> задач")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not _is_owner(message):
        return
    parts = (message.text or "").split()
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


@router.message(Command("priority"))
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


@router.message(Command("experts_today"))
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


@router.message(Command("cleanup_junk_predictions"))
async def cmd_cleanup_junk(message: Message):
    """Удаляет «пустые» прогнозы, созданные до ужесточения промпта."""
    if not _is_owner(message):
        return

    junk_markers = (
        "о котором говорится в интервью",
        "событие, о котором",
    )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    all_rows = conn.execute(
        "SELECT id, prediction_text FROM predictions"
    ).fetchall()

    to_delete = []
    for r in all_rows:
        text_low = (r["prediction_text"] or "").lower()
        if any(m in text_low for m in junk_markers):
            to_delete.append(r["id"])

    if not to_delete:
        conn.close()
        await message.answer("✅ «Пустышек» не найдено")
        return

    placeholders = ",".join("?" * len(to_delete))
    conn.execute(f"DELETE FROM predictions WHERE id IN ({placeholders})", to_delete)
    conn.commit()
    conn.close()

    await message.answer(
        f"🧹 Удалено «пустышек»: <b>{len(to_delete)}</b>\n"
        f"ID: {', '.join(f'#{i}' for i in to_delete[:20])}"
        + ("…" if len(to_delete) > 20 else "")
    )