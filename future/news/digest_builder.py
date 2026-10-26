"""
Сборка новостного дайджеста через LLM.

Этот модуль — задел под будущий новостной блок.
Сейчас — заглушка, чтобы структура была готова.

Как это будет работать:
  1. На вход — список новостей (из rss_reader.fetch_news).
  2. Группировка по темам (через LLM или эвристики).
  3. Сборка краткого дайджеста (5-10 пунктов, каждый 1-2 предложения).
  4. Атрибуция: «по данным <source>».
  5. Возврат строки в формате HTML для Telegram.

Интеграция (будущая):
  - Вызывается из `workers/scheduler.py` раз в N часов.
  - Публикуется через `publishers/telegram.py` в тот же канал
    (или в отдельный, если так решат).

Ничего в существующих модулях при этом меняться не будет.
"""

from typing import Any


async def build_digest(
    news_items: list[dict[str, Any]],
    max_items: int = 10,
) -> str:
    """
    Собирает дайджест из списка новостей через LLM.

    Args:
        news_items: список новостей из fetch_news.
        max_items: максимум пунктов в дайджесте.

    Returns:
        HTML-строка для Telegram.
    """
    raise NotImplementedError(
        "future.news.digest_builder.build_digest — в разработке. "
        "См. docstring модуля."
    )