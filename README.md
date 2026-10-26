# ГЕОПОЛИТРУК — YouTube → Telegram

Бот собирает аналитические выжимки из YouTube-интервью, публикует в Telegram-канал
и накапливает прогнозы экспертов для будущего анализа.

## Структура проекта

├── bot.py # Точка входа (тонкая): init, роутеры, start_polling
├── config.py # Все константы и env-переменные
│
├── db/ # SQLite
│ ├── connection.py # init_db, миграции, очистка temp
│ ├── queue.py # CRUD task_queue
│ ├── predictions.py # CRUD predictions + list/get/update/delete
│ ├── summaries.py # CRUD video_summaries
│ ├── experts.py # experts_seen_today
│ └── videos.py # processed_videos
│
├── services/ # Бизнес-логика (без Telegram и БД)
│ ├── llm.py # Единый Groq-клиент
│ ├── youtube_api.py # Единый YouTube Data API
│ ├── youtube.py # RSS-first, API-fallback, кэши, квоты, yt-dlp
│ ├── transcription.py # download_audio + Groq Whisper / локальный
│ ├── expert_detection.py # Эвристики + LLM-детекция эксперта
│ ├── summarization.py # Map-Reduce суммаризация + фикс 413
│ └── predictions_extraction.py # Извлечение прогнозов + фикс 413
│
├── handlers/ # Telegram
│ ├── admin.py # /start /stats /queue /errors /reset /cancel /priority ...
│ ├── predictions.py # /predictions + inline-кнопки + FSM
│ └── links.py # Обработка YouTube-ссылок от владельца
│
├── workers/ # Фоновые задачи
│ ├── task_worker.py # _worker_loop — обработка очереди
│ └── scheduler.py # check_all_channels + heartbeat
│
├── publishers/
│ └── telegram.py # send_expert_post + process_and_publish
│
├── future/ # Задел под будущие модули
│ └── news/ # Новостной блок (заглушки)
│
├── experts.py # Словарь экспертов (KNOWN_EXPERT_BIOS/FULL)
└── requirements.txt

"""
## Запуск локально

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# .env в корне:
# BOT_TOKEN=...
# GROQ_API_KEY=...
# CHANNEL_ID=-100...
# OWNER_USER_ID=...
# GOOGLE_CREDENTIALS_JSON={...}  (одной строкой)
# DB_PATH=seen_videos.db

python bot.py

CLI-команды
Команда	Что делает
python bot.py	Запуск бота
python bot.py --queue	Показать очередь
python bot.py --errors	Показать последние ошибки
python bot.py --predictions	Показать прогнозы
python bot.py --mark <id> <verdict>	Установить вердикт
python bot.py --cancel <id>	Отменить задачу
python bot.py --reset	Сбросить очередь
python bot.py --process <url>	Обработать одно видео (без очереди)"""

Telegram-команды (владелец)
Команда	Что делает
/start	Список команд
/queue	Очередь
/stats	Статистика + RAM
/quota	Квота YouTube
/errors	Последние ошибки
/predictions	Прогнозы с inline-кнопками
/pred <id>	Детали прогноза
/editpred <id> <текст>	Быстрое редактирование
/verdict <id> <v>	Установить вердикт
/delpred <id>	Удалить прогноз
/priority	Рейтинг экспертов
/experts_today	Кто выступал сегодня
/reset	Сбросить очередь
/cancel <id>	Отменить задачу
/cleanup_junk_predictions	Удалить «пустышки» из прогнозов

Деплой на Bothost
Git URL: https://github.com/yggris-netizen/Geopolitryk.git

Ветка: main

Главный файл: bot.py

Обязательно: DB_PATH=/app/data/seen_videos.db — иначе БД теряется при перезапуске.

Остальные переменные: BOT_TOKEN, GROQ_API_KEY, CHANNEL_ID, OWNER_USER_ID, GOOGLE_CREDENTIALS_JSON, WHISPER_MODE=groq.

Развитие