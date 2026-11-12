# -*- coding: utf-8 -*-
"""Заглушка хранилища (LocalStorage). Потом заменится на S3/MinIO.

НЕ импортируется текущим ботом. Готовит абстракцию для миграции.
"""
import json
import os
import shutil


class LocalStorage:
    """Локальное хранилище: audio/ и results/ в корне проекта."""

    def __init__(self, base_dir: str = None):
        self.base = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.audio_dir = os.path.join(self.base, "storage", "audio")
        self.results_dir = os.path.join(self.base, "storage", "results")
        os.makedirs(self.audio_dir, exist_ok=True)
        os.makedirs(self.results_dir, exist_ok=True)

    def save_audio(self, video_id: str, src_path: str) -> str:
        dst = os.path.join(self.audio_dir, f"{video_id}.mp3")
        shutil.copy2(src_path, dst)
        return dst

    def audio_path(self, video_id: str) -> str:
        return os.path.join(self.audio_dir, f"{video_id}.mp3")

    def audio_exists(self, video_id: str) -> bool:
        return os.path.exists(self.audio_path(video_id))

    def save_result(self, video_id: str, data: dict) -> str:
        dst = os.path.join(self.results_dir, f"{video_id}.json")
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return dst

    def load_result(self, video_id: str) -> dict | None:
        src = os.path.join(self.results_dir, f"{video_id}.json")
        if not os.path.exists(src):
            return None
        with open(src, "r", encoding="utf-8") as f:
            return json.load(f)
