"""Обработка YouTube-ссылок от владельца."""

from aiogram import Router, F
from aiogram.types import Message

from db.queue import _add_task_to_queue
from db.videos import _is_video_processed
from services.youtube import _is_youtube_url, _extract_youtube_url, _extract_video_id


router = Router()

_OWNER_USER_ID = None


def set_owner_user_id(owner_id: int):
    global _OWNER_USER_ID
    _OWNER_USER_ID = owner_id


def _is_owner(message: Message) -> bool:
    return message.from_user and message.from_user.id == _OWNER_USER_ID


@router.message(F.text)
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
