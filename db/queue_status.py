# -*- coding: utf-8 -*-
"""Статусы очереди для миграции (Downloader -> Processor -> Publisher).

НЕ импортируется текущим ботом. Готовит единый словарь статусов.
"""

STATUS_PENDING = "pending"        # создана, ждёт скачивания
STATUS_UPLOADED = "uploaded"      # аудио скачано и загружено в storage
STATUS_PROCESSED = "processed"    # транскрипт + анализ готовы
STATUS_PUBLISHED = "published"    # опубликовано в Telegram
STATUS_ERROR = "error"            # ошибка (с причиной)

ALL_STATUSES = (
    STATUS_PENDING, STATUS_UPLOADED,
    STATUS_PROCESSED, STATUS_PUBLISHED, STATUS_ERROR,
)
