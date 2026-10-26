"""CRUD прогнозов через Telegram: просмотр, редактирование, вердикты, удаление."""

import html
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from db.predictions import (
    get_prediction, update_prediction_text, update_prediction_verdict,
    delete_prediction, list_predictions,
)


router = Router()

_OWNER_USER_ID = None
PAGE_SIZE = 5


def set_owner_user_id(owner_id: int):
    global _OWNER_USER_ID
    _OWNER_USER_ID = owner_id


def _is_owner(message_or_cb) -> bool:
    user = message_or_cb.from_user
    return user and user.id == _OWNER_USER_ID


class PredStates(StatesGroup):
    waiting_new_text = State()
    waiting_delete_confirm = State()


def _verdict_icon(verdict):
    return {"confirmed": "✅", "refuted": "❌",
            "unverifiable": "❔"}.get(verdict, "⏳")


def _verdict_label(verdict):
    return {
        "confirmed": "Подтверждён",
        "refuted": "Опровергнут",
        "unverifiable": "Непроверяемый",
        None: "Без вердикта",
        "": "Без вердикта",
    }.get(verdict, verdict)


def _format_prediction_short(p):
    icon = _verdict_icon(p.get("verdict"))
    text = (p.get("prediction_text") or "")[:200]
    expert = p.get("expert_name") or "?"
    return f"{icon} <b>#{p['id']}</b> — {html.escape(expert)}\n{html.escape(text)}"


def _format_prediction_full(p):
    lines = [
        f"<b>🔮 Прогноз #{p['id']}</b>\n",
        f"<b>Эксперт:</b> {html.escape(p.get('expert_name') or '?')}",
        f"<b>Канал:</b> {html.escape(p.get('channel_name') or '—')}",
        f"<b>Дата:</b> {p.get('prediction_date') or '—'}",
        f"<b>Вердикт:</b> {_verdict_label(p.get('verdict'))}",
        "",
        "<b>Текст:</b>",
        html.escape(p.get('prediction_text') or '—'),
    ]
    if p.get("verdict_notes"):
        lines += ["", f"<b>Заметки:</b> {html.escape(p['verdict_notes'])}"]
    return "\n".join(lines)


def _prediction_keyboard(pred_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"pred:edit:{pred_id}"),
            InlineKeyboardButton(text="✅ Вердикт", callback_data=f"pred:verdict:{pred_id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"pred:delete:{pred_id}"),
            InlineKeyboardButton(text="📄 Детали", callback_data=f"pred:show:{pred_id}"),
        ],
    ])


def _verdict_keyboard(pred_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтверждён", callback_data=f"pred:setv:{pred_id}:confirmed"),
            InlineKeyboardButton(text="❌ Опровергнут", callback_data=f"pred:setv:{pred_id}:refuted"),
        ],
        [
            InlineKeyboardButton(text="❔ Непроверяемый", callback_data=f"pred:setv:{pred_id}:unverifiable"),
            InlineKeyboardButton(text="⏳ Сбросить", callback_data=f"pred:setv:{pred_id}:pending"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"pred:show:{pred_id}")],
    ])


def _list_keyboard(page: int, total: int, expert: str | None, verdict: str | None):
    buttons = []
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"pred:page:{page-1}"))
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    nav_row.append(InlineKeyboardButton(text=f"{page+1}/{max(total_pages,1)}", callback_data="pred:noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"pred:page:{page+1}"))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([
        InlineKeyboardButton(text="⏳ Только без вердикта", callback_data="pred:filter:pending"),
        InlineKeyboardButton(text="🔄 Сбросить фильтр", callback_data="pred:filter:all"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_list(message_or_cb, page=0, expert=None, verdict=None):
    rows, total = list_predictions(limit=PAGE_SIZE, offset=page * PAGE_SIZE,
                                    expert=expert, verdict=verdict)
    if not rows:
        text = "📭 Прогнозов пока нет"
        if isinstance(message_or_cb, CallbackQuery):
            await message_or_cb.message.edit_text(text)
        else:
            await message_or_cb.answer(text)
        return

    lines = [f"<b>🔮 Прогнозы ({total} всего, стр. {page+1}):</b>\n"]
    for p in rows:
        lines.append(_format_prediction_short(p))
        lines.append("")  # пустая строка между прогнозами
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "…"

    kb = _list_keyboard(page, total, expert, verdict)

    if isinstance(message_or_cb, CallbackQuery):
        await message_or_cb.message.edit_text(text, reply_markup=kb)
    else:
        await message_or_cb.answer(text, reply_markup=kb)


@router.message(Command("predictions"))
async def cmd_predictions(message: Message):
    if not _is_owner(message):
        return
    await _render_list(message, page=0)


@router.message(Command("pred"))
async def cmd_pred(message: Message):
    if not _is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /pred &lt;id&gt;")
        return
    try:
        pid = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    p = get_prediction(pid)
    if not p:
        await message.answer(f"❌ Прогноз #{pid} не найден")
        return
    await message.answer(_format_prediction_full(p), reply_markup=_prediction_keyboard(pid))


@router.message(Command("editpred"))
async def cmd_editpred(message: Message):
    if not _is_owner(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /editpred &lt;id&gt; &lt;новый текст&gt;")
        return
    try:
        pid = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    new_text = parts[2].strip()
    if not update_prediction_text(pid, new_text):
        await message.answer("❌ Не удалось обновить (текст пустой, >400 символов или ID не найден)")
        return
    p = get_prediction(pid)
    await message.answer(f"✅ Прогноз #{pid} обновлён:\n\n{html.escape(p['prediction_text'])}",
                         reply_markup=_prediction_keyboard(pid))


@router.message(Command("verdict"))
async def cmd_verdict(message: Message):
    if not _is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Использование: /verdict &lt;id&gt; confirmed|refuted|unverifiable|pending"
        )
        return
    try:
        pid = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    v = parts[2].lower()
    if v not in ("confirmed", "refuted", "unverifiable", "pending"):
        await message.answer("❌ Вердикт: confirmed, refuted, unverifiable или pending")
        return
    ok = update_prediction_verdict(pid, "" if v == "pending" else v)
    if not ok:
        await message.answer(f"❌ Не удалось обновить прогноз #{pid}")
        return
    p = get_prediction(pid)
    await message.answer(
        f"✅ #{pid} → {_verdict_label(p.get('verdict'))}",
        reply_markup=_prediction_keyboard(pid)
    )


@router.message(Command("delpred"))
async def cmd_delpred(message: Message):
    if not _is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /delpred &lt;id&gt;")
        return
    try:
        pid = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    p = get_prediction(pid)
    if not p:
        await message.answer(f"❌ Прогноз #{pid} не найден")
        return
    if delete_prediction(pid):
        await message.answer(f"🗑 Прогноз #{pid} удалён")
    else:
        await message.answer(f"❌ Не удалось удалить #{pid}")


# ─── Callback-кнопки ─────────────────────────────────────

@router.callback_query(F.data == "pred:noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("pred:page:"))
async def cb_page(callback: CallbackQuery):
    if not _is_owner(callback):
        await callback.answer("⛔", show_alert=False)
        return
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    await _render_list(callback, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("pred:filter:"))
async def cb_filter(callback: CallbackQuery):
    if not _is_owner(callback):
        await callback.answer("⛔", show_alert=False)
        return
    filt = callback.data.split(":")[2]
    verdict = "pending" if filt == "pending" else None
    await _render_list(callback, page=0, verdict=verdict)
    await callback.answer()


@router.callback_query(F.data.startswith("pred:show:"))
async def cb_show(callback: CallbackQuery):
    if not _is_owner(callback):
        await callback.answer("⛔", show_alert=False)
        return
    try:
        pid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    p = get_prediction(pid)
    if not p:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    await callback.message.edit_text(_format_prediction_full(p),
                                      reply_markup=_prediction_keyboard(pid))
    await callback.answer()


@router.callback_query(F.data.startswith("pred:edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback):
        await callback.answer("⛔", show_alert=False)
        return
    try:
        pid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    p = get_prediction(pid)
    if not p:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    await state.set_state(PredStates.waiting_new_text)
    await state.update_data(pred_id=pid)
    await callback.message.answer(
        f"✏️ Редактирование прогноза #{pid}\n\n"
        f"Текущий текст:\n<i>{html.escape(p['prediction_text'])}</i>\n\n"
        f"Отправьте новый текст (до 400 символов) или /cancel для отмены."
    )
    await callback.answer()


@router.message(PredStates.waiting_new_text, F.text == "/cancel")
async def cancel_edit(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("⏹ Редактирование отменено")


@router.message(PredStates.waiting_new_text, F.text)
async def save_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("pred_id")
    if not pid:
        await state.clear()
        return
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("❌ Пустой текст. Отправьте текст или /cancel.")
        return
    if len(new_text) > 400:
        await message.answer(f"❌ Текст слишком длинный ({len(new_text)} > 400). Сократите или /cancel.")
        return
    if not update_prediction_text(pid, new_text):
        await message.answer("❌ Не удалось обновить прогноз")
        await state.clear()
        return
    await state.clear()
    p = get_prediction(pid)
    await message.answer(
        f"✅ Прогноз #{pid} обновлён:\n\n{html.escape(p['prediction_text'])}",
        reply_markup=_prediction_keyboard(pid)
    )


@router.callback_query(F.data.startswith("pred:verdict:"))
async def cb_verdict_menu(callback: CallbackQuery):
    if not _is_owner(callback):
        await callback.answer("⛔", show_alert=False)
        return
    try:
        pid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    p = get_prediction(pid)
    if not p:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    await callback.message.edit_text(
        _format_prediction_full(p),
        reply_markup=_verdict_keyboard(pid)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pred:setv:"))
async def cb_set_verdict(callback: CallbackQuery):
    if not _is_owner(callback):
        await callback.answer("⛔", show_alert=False)
        return
    try:
        _, _, pid_s, v = callback.data.split(":")
        pid = int(pid_s)
    except (ValueError, IndexError):
        await callback.answer()
        return
    ok = update_prediction_verdict(pid, "" if v == "pending" else v)
    if not ok:
        await callback.answer("❌ Не удалось", show_alert=True)
        return
    p = get_prediction(pid)
    await callback.message.edit_text(
        _format_prediction_full(p),
        reply_markup=_prediction_keyboard(pid)
    )
    await callback.answer(f"✅ {_verdict_label(p.get('verdict'))}")


@router.callback_query(F.data.startswith("pred:delete:"))
async def cb_delete_confirm(callback: CallbackQuery):
    if not _is_owner(callback):
        await callback.answer("⛔", show_alert=False)
        return
    try:
        pid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    p = get_prediction(pid)
    if not p:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"pred:delyes:{pid}"),
            InlineKeyboardButton(text="◀️ Отмена", callback_data=f"pred:show:{pid}"),
        ]
    ])
    await callback.message.edit_text(
        f"🗑 Удалить прогноз #{pid}?\n\n<i>{html.escape(p['prediction_text'][:300])}</i>",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pred:delyes:"))
async def cb_delete_yes(callback: CallbackQuery):
    if not _is_owner(callback):
        await callback.answer("⛔", show_alert=False)
        return
    try:
        pid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    if delete_prediction(pid):
        await callback.message.edit_text(f"🗑 Прогноз #{pid} удалён")
        await callback.answer("Удалено")
    else:
        await callback.answer("❌ Не удалось", show_alert=True)