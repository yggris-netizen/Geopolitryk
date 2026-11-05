"""FSM-хендлер для разметки незнакомых голосов.

Владелец получает уведомление с inline-кнопками:
[👤 Эксперт] [🌐 Ведущий] [⏭ Пропустить]

Если эксперт → FSM: ввод ФИО → ввод специализации → сохранить в voice_profiles.
Если ведущий → FSM: ввод ФИО → сохранить в voice_profiles (role=author).
Если пропустить → mark_resolved(ignored).
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from config import OWNER_USER_ID
from db.pending_voices import get_pending_by_id, mark_resolved
from db.voice_profiles import save_voice_profile

router = Router()


class VoiceStates(StatesGroup):
    waiting_name = State()
    waiting_bio = State()


def _is_owner(user_id: int) -> bool:
    return user_id == OWNER_USER_ID


def build_unknown_notification(video_url: str, video_title: str,
                                pending_list: list) -> tuple:
    """Формирует текст + inline-кнопки для уведомления о незнакомых голосах.

    pending_list: [{id, speaker_id, duration_sec, role_guess}, ...]
    Возвращает (text, InlineKeyboardMarkup).
    """
    lines = [
        "🎤 <b>Незнакомые голоса</b>",
        f"📺 {video_title[:80]}",
        f"🔗 {video_url}",
        "",
        "Разметьте каждый голос:",
    ]
    for i, p in enumerate(pending_list, 1):
        dur = p.get("duration_sec", 0)
        lines.append(f"\n{i}. <b>{p.get('speaker_id', '?')}</b>, {dur:.0f}с")

    keyboard = []
    for p in pending_list:
        pid = p["id"]
        spk = p.get("speaker_id", "?")
        keyboard.append([
            InlineKeyboardButton(text=f"👤 {spk} — Эксперт", callback_data=f"voice:guest:{pid}"),
            InlineKeyboardButton(text=f"🌐 {spk} — Ведущий", callback_data=f"voice:author:{pid}"),
        ])
        keyboard.append([
            InlineKeyboardButton(text=f"⏭ {spk} — Пропустить", callback_data=f"voice:skip:{pid}"),
        ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.callback_query(F.data.startswith("voice:"))
async def on_voice_role(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer("⛔ Только владелец", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ Неверный формат", show_alert=True)
        return

    _, role, pid_str = parts
    try:
        pid = int(pid_str)
    except ValueError:
        await callback.answer("❌ Неверный ID", show_alert=True)
        return

    pending = get_pending_by_id(pid)
    if not pending:
        await callback.answer("❌ Голос не найден (уже размечен?)", show_alert=True)
        return

    if role == "skip":
        mark_resolved(pid, "ignored")
        await callback.message.edit_text(
            callback.message.text + f"\n\n⏭ {pending.get('speaker_id', '?')} — пропущен"
        )
        await callback.answer("⏭ Пропущено")
        return

    if role not in ("guest", "author"):
        await callback.answer("❌ Неверная роль", show_alert=True)
        return

    # Сохраняем в FSM: pid, role, message_id
    await state.update_data(pending_id=pid, role=role)
    await state.set_state(VoiceStates.waiting_name)

    role_ru = "эксперта" if role == "guest" else "ведущего"
    await callback.message.edit_text(
        callback.message.text + f"\n\n✏️ Введите ФИО {role_ru} для {pending.get('speaker_id', '?')}:"
    )
    await callback.answer(f"Введите ФИО {role_ru}")


@router.message(VoiceStates.waiting_name)
async def on_voice_name(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return

    name = (message.text or "").strip()
    if not name or len(name) < 3 or len(name) > 60:
        await message.answer("❌ ФИО: от 3 до 60 символов. Попробуйте ещё раз:")
        return

    data = await state.get_data()
    role = data.get("role", "guest")

    if role == "author":
        # Ведущий — сохраняем сразу (без специализации)
        pid = data.get("pending_id")
        pending = get_pending_by_id(pid) if pid else None
        if not pending:
            await message.answer("❌ Голос не найден")
            await state.clear()
            return
        save_voice_profile(
            name=name, bio="", role="author",
            embedding=pending["embedding"],
            duration_sec=pending.get("duration_sec", 0),
            source_video_id=pending.get("video_id", ""),
        )
        mark_resolved(pid, "resolved")
        await message.answer(f"✅ Ведущий <b>{name}</b> сохранён (роль: author)")
        await state.clear()
        return

    # Эксперт — запрашиваем специализацию
    await state.update_data(name=name)
    await state.set_state(VoiceStates.waiting_bio)
    await message.answer(
        f"✅ ФИО: <b>{name}</b>\n\n"
        f"Теперь введите специализацию (например: политолог, экономист, военный аналитик). "
        f"Или отправьте <code>-</code>, чтобы пропустить:"
    )


@router.message(VoiceStates.waiting_bio)
async def on_voice_bio(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return

    bio = (message.text or "").strip()
    if bio == "-":
        bio = ""
    elif bio and (len(bio) < 3 or len(bio) > 60):
        await message.answer("❌ Специализация: от 3 до 60 символов, или <code>-</code>. Попробуйте ещё раз:")
        return

    data = await state.get_data()
    pid = data.get("pending_id")
    name = data.get("name", "")
    if not pid or not name:
        await message.answer("❌ Ошибка: данные разметки потеряны")
        await state.clear()
        return

    pending = get_pending_by_id(pid)
    if not pending:
        await message.answer("❌ Голос не найден")
        await state.clear()
        return

    save_voice_profile(
        name=name, bio=bio, role="guest",
        embedding=pending["embedding"],
        duration_sec=pending.get("duration_sec", 0),
        source_video_id=pending.get("video_id", ""),
    )
    mark_resolved(pid, "resolved")
    bio_str = f" — {bio}" if bio else ""
    await message.answer(f"✅ Эксперт <b>{name}</b>{bio_str} сохранён (роль: guest)")
    await state.clear()