"""
Чтение RSS новостных лент (НЕ YouTube).

Этот модуль — задел под будущий новостной блок.
Сейчас — заглушка, чтобы структура была готова.

Как это будет работать:
  1. Список новостных RSS-лент (список словарей {url, name}).
  2. Парсинг каждого фида через feedparser или xml.etree.
  3. Фильтрация по дате (аналог HOURS_BACK для YouTube).
  4. Дедупликация по URL.
  5. Возврат списка [{title, url, summary, published_at, source}].

Интеграция (будущая):
  - `handlers/news.py` — команды /news, /news_digest, /news_settings
  - `db/news.py` — история увиденных новостей
  - `workers/scheduler.py` — периодическая сборка дайджеста
  - `services/news_digest.py` — сборка дайджеста через LLM

Ничего в существующих модулях (`bot.py`, `db/`, `services/`, `handlers/`, `workers/`, `publishers/`)
при этом меняться не будет — только добавление новых файлов.
"""

from typing import Any


async def fetch_news(feed_urls: list[str], hours_back: int = 12) -> list[dict[str, Any]]:
    """
    Читает RSS-ленты и возвращает новые новости за последние `hours_back` часов.

    Args:
        feed_urls: список URL RSS-лент.
        hours_back: сколько часов назад смотреть (по умолчанию 12).

    Returns:
        Список словарей: [{title, url, summary, published_at, source}].
    """
    raise NotImplementedError(
        "future.news.rss_reader.fetch_news — в разработке. "
        "См. docstring модуля."
    )