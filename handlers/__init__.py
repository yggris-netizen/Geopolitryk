"""Регистрация всех роутеров."""

from handlers.admin import router as admin_router
from handlers.links import router as links_router
from handlers.predictions import router as predictions_router
from handlers.voice_registration import router as voice_registration_router

__all__ = [
    "admin_router",
    "links_router",
    "predictions_router",
    "voice_registration_router",
]