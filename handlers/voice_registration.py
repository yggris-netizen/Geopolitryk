"""FSM-хендлер для разметки незнакомых голосов.

Уведомление:
✅ Уже распознаны: A, B (если есть)
🎤 Незнакомые голоса: ...
[✅ Спикер] [⏭ Пропустить]

При «Спикер» → FSM: только ФИО → сохранить в voice_profiles (role="guest").
При «Пропустить» → mark_resolved(ignored).

ВАЖНО: callback.answer() вызывается ПЕРВЫМ (лимит Telegram 15 сек).
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


def _is_owner(user_id: int) -> bool:
    return user_id == OWNER_USER_ID


def build_unknown_notification(video_url: str, video_title: str,
                                pending_list: list,
                                known_experts: list = None) -> tuple:
    """Текст + inline-кнопки.

    known_experts: [{"name": "..."}] — уже распознанные в этом видео.
    pending_list: [{"id", "speaker_id", "duration_sec"}] — незнакомые.
    """
    lines = [
        "🎤 <b>Незнакомые голоса</b>",
        f"📺 {video_title[:80]}",
        f"🔗 {video_url}",
    ]

    # Блок «Уже распознаны» — чтобы не размечать повторно
    if known_experts:
        names = [e["name"] for e in known_experts if e.get("name")]
        if names:
            lines.append("")
            lines.append(f"✅ <b>Уже распознаны:</b> {', '.join(names)}")

    lines.append("")
    lines.append("Разметьте каждый незнакомый голос:")
    for i, p in enumerate(pending_list, 1):
        dur = p.get("duration_sec", 0)
        spk = p.get("speaker_id") or "?"
        lines.append(f"\n{i}. <b>{spk}</b>, {dur:.0f}с")

    keyboard = []
    for p in pending_list:
        pid = p["id"]
        spk = p.get("speaker_id") or "?"
        keyboard.append([
            InlineKeyboardButton(text=f"✅ {spk} — Спикер",
                                 callback_data=f"voice:speaker:{pid}"),
            InlineKeyboardButton(text=f"⏭ {spk} — Пропустить",
                                 callback_data=f"voice:skip:{pid}"),
        ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.callback_query(F.data.startswith("voice:"))
async def on_voice_role(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer("⛔ Только владелец", show_alert=True)
        return
    await callback.answer()

    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    _, role, pid_str = parts
    try:
        pid = int(pid_str)
    except ValueError:
        return

    pending = get_pending_by_id(pid)
    if not pending:
        try:
            await callback.message.edit_text(
                callback.message.text + "\n\n❌ Голос не найден (уже размечен?)"
            )
        except Exception:
            pass
        return

    spk = pending.get("speaker_id") or "?"

    if role == "skip":
        mark_resolved(pid, "ignored")
        try:
            await callback.message.edit_text(
                callback.message.text + f"\n\n⏭ {spk} — пропущен"
            )
        except Exception:
            pass
        return

    if role != "speaker":
        return

    await state.update_data(pending_id=pid, speaker_id=spk)
    await state.set_state(VoiceStates.waiting_name)

    try:
        await callback.message.edit_text(
            callback.message.text + f"\n\n✏️ Введите ФИО спикера для {spk}:"
        )
    except Exception:
        pass


@router.message(VoiceStates.waiting_name)
async def on_voice_name(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return

    name = (message.text or "").strip()
    if not name or len(name) < 3 or len(name) > 60:
        await message.answer("❌ ФИО: от 3 до 60 символов. Попробуйте ещё раз:")
        return

    data = await state.get_data()
    pid = data.get("pending_id")
    spk = data.get("speaker_id", "?")
    pending = get_pending_by_id(pid) if pid else None
    if not pending:
        await message.answer("❌ Голос не найден")
        await state.clear()
        return

    save_voice_profile(
        name=name, bio="", role="guest",
        embedding=pending["embedding"],
        duration_sec=pending.get("duration_sec", 0),
        source_video_id=pending.get("video_id", ""),
    )
    mark_resolved(pid, "resolved")
    await message.answer(f"✅ Спикер <b>{name}</b> ({spk}) сохранён")
    await state.clear()