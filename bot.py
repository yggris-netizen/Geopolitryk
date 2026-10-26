"""
YouTube → Telegram бот: аналитические выжимки.
Точка входа: инициализация, роутеры, воркер, планировщик.

После Этапа 3 декомпозиции:
  - handlers/     — Telegram-команды и обработка ссылок
  - workers/      — фоновый воркер и планировщик
  - publishers/   — публикация в канал
  - services/     — YouTube, транскрибация, эксперты, суммаризация, прогнозы
  - db/           — SQLite
  - config.py     — константы и env
"""

import asyncio
import os
import signal
import sqlite3
import sys
import traceback

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import (
    BOT_TOKEN, CHANNEL_ID, DB_PATH, GROQ_API_KEY, GROQ_MODEL,
    GROQ_MAX_CHUNK_SECONDS, GROQ_MAX_SIZE_MB, GROQ_WHISPER_MODEL,
    HEARTBEAT_HOURS, HOURS_BACK, MAX_TASK_ATTEMPTS,
    MAX_VIDEOS_PER_CHANNEL_PER_CHECK, OWNER_USER_ID,
    SERVICE_ACCOUNT_FILE, TEST_VIDEO_TITLE, TEST_VIDEO_URL,
    WHISPER_MODE, WHISPER_MODEL_SIZE, CHANNELS_TO_WATCH,
    CHECK_INTERVAL_HOURS, DIAGNOSTIC_MODE,
)
from experts import KNOWN_EXPERT_BIOS, KNOWN_EXPERT_FULL

from db.connection import _init_db, _cleanup_temp_files
from db.queue import (
    _queue_list, _recent_errors, _cancel_task, _reset_queue,
)
from db.summaries import _save_summary  # noqa: F401 (для CLI совместимости)
from db.predictions import _recent_predictions  # noqa: F401
from services.youtube import get_video_info_ytdlp

from handlers import admin_router, links_router
from handlers.admin import set_owner_user_id as set_admin_owner
from handlers.links import set_owner_user_id as set_links_owner
from publishers.telegram import process_and_publish, set_bot
from workers.task_worker import _worker_loop
from workers.scheduler import check_all_channels, build_scheduler


_SHUTDOWN_EVENT = None


# ╔══════════════════════════════════════════════════════════╗
# ║              ИНИЦИАЛИЗАЦИЯ                               ║
# ╚══════════════════════════════════════════════════════════╝

print("=" * 60)
print("ЗАПУСК БОТА")
print("=" * 60)

_errors = []
if not BOT_TOKEN:
    _errors.append("❌ BOT_TOKEN не задан")
if not GROQ_API_KEY:
    _errors.append("❌ GROQ_API_KEY не задан")
if not isinstance(CHANNEL_ID, int) or CHANNEL_ID >= 0:
    _errors.append("❌ CHANNEL_ID должен быть отрицательным")
if not isinstance(OWNER_USER_ID, int) or OWNER_USER_ID <= 0:
    _errors.append("❌ OWNER_USER_ID должен быть положительным")
if not os.path.exists(SERVICE_ACCOUNT_FILE):
    _errors.append(f"❌ {SERVICE_ACCOUNT_FILE} не найден")
if WHISPER_MODE not in ("groq", "local"):
    _errors.append(f"❌ WHISPER_MODE: '{WHISPER_MODE}'")

for _ch in CHANNELS_TO_WATCH:
    if _ch["id"] != _ch["id"].strip():
        _errors.append(f"❌ ID «{_ch['name']}» содержит пробелы")

if _errors:
    print("\n⚠️  ОШИБКИ:\n")
    for _e in _errors:
        print(f"  {_e}")
    sys.exit(1)

print("✅ Настройки в порядке")
print(f"   Канал:            {CHANNEL_ID}")
print(f"   Owner:            {OWNER_USER_ID}")
print(f"   Модель LLM:       {GROQ_MODEL}")
print(f"   Режим Whisper:    {WHISPER_MODE}")
if WHISPER_MODE == "groq":
    print(f"   Groq Whisper:     {GROQ_WHISPER_MODEL}")
    print(f"   Нарезка:          {GROQ_MAX_CHUNK_SECONDS}с")
    print(f"   Лимит файла:      {GROQ_MAX_SIZE_MB} MB")
else:
    print(f"   Локальная:        {WHISPER_MODEL_SIZE}")
print(f"   Каналов:          {len(CHANNELS_TO_WATCH)}")
print(f"   Глубина поиска:   {HOURS_BACK} ч (без фолбэка)")
print(f"   Лимит видео/канал: {MAX_VIDEOS_PER_CHANNEL_PER_CHECK}")
print(f"   Max attempts:     {MAX_TASK_ATTEMPTS}")
print(f"   БД:               {DB_PATH}")
print(f"   Словарь экспертов: {len(KNOWN_EXPERT_BIOS)} записей, "
      f"{len(KNOWN_EXPERT_FULL)} расширений")
if HEARTBEAT_HOURS > 0:
    print(f"   Heartbeat:        каждые {HEARTBEAT_HOURS} ч")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.include_routers(admin_router, links_router)

set_admin_owner(OWNER_USER_ID)
set_links_owner(OWNER_USER_ID)
set_bot(bot)
print()


# ╔══════════════════════════════════════════════════════════╗
# ║                       ЗАПУСК                             ║
# ╚══════════════════════════════════════════════════════════╝

async def _main():
    global _SHUTDOWN_EVENT
    _SHUTDOWN_EVENT = asyncio.Event()

    print("🔍 [1/10] _main() стартовал")
    print(f"🔍 sys.argv = {sys.argv}")
    print(f"🔍 CWD = {os.getcwd()}")

    def _handle_shutdown_signal():
        print("\n⏹ Получен сигнал завершения")
        _SHUTDOWN_EVENT.set()

    try:
        loop = asyncio.get_running_loop()
        print(f"🔍 [2/10] Running loop: {type(loop).__name__}")
    except Exception as e:
        print(f"⚠️  get_running_loop: {e}")

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_shutdown_signal)
                print(f"🔍 signal {sig.name} зарегистрирован")
            except (NotImplementedError, RuntimeError, ValueError) as _e:
                print(f"🔍 signal {sig.name} не зарегистрирован ({type(_e).__name__})")
    except Exception as e:
        print(f"⚠️  signal setup: {e}")

    print("🔍 [3/10] CLI checks")
    if "--predictions" in sys.argv:
        _init_db()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM predictions ORDER BY prediction_date DESC LIMIT 50"
        ).fetchall()
        conn.close()
        if not rows:
            print("Нет прогнозов")
            return
        for r in rows:
            m = {"confirmed": "✅", "refuted": "❌", "unverifiable": "❔"}.get(r["verdict"], "⏳")
            print(f"{m} [{r['id']}] {r['expert_name']}: {r['prediction_text'][:100]}")
        return

    if "--mark" in sys.argv:
        _init_db()
        idx = sys.argv.index("--mark")
        try:
            pid = int(sys.argv[idx + 1])
            v = sys.argv[idx + 2]
        except (IndexError, ValueError):
            print("python bot.py --mark <id> confirmed|refuted|unverifiable")
            return
        if v not in ("confirmed", "refuted", "unverifiable"):
            print("❌ Неверный вердикт")
            return
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE predictions SET verdict = ?, verdict_date = CURRENT_TIMESTAMP WHERE id = ?",
            (v, pid)
        )
        conn.commit()
        conn.close()
        print(f"✅ [{pid}] → {v}")
        return

    if "--queue" in sys.argv:
        _init_db()
        for t in _queue_list():
            ic = "🔄" if t['status'] == 'processing' else "⏳"
            src = "👤" if t['source'] == 'telegram' else "🤖"
            print(f"{ic}{src} #{t['id']} [P{t['priority']}] "
                  f"(попыток: {t['attempts']}) "
                  f"{(t['video_title'] or t['video_url'])[:60]}")
        return

    if "--errors" in sys.argv:
        _init_db()
        errs = _recent_errors(20)
        if not errs:
            print("✅ Ошибок нет")
            return
        for e in errs:
            print(f"❌ #{e['id']} (попыток: {e['attempts']}): "
                  f"{(e['video_title'] or e['video_url'])[:50]}")
            print(f"   {e['error_message'] or '—'}")
        return

    if "--cancel" in sys.argv:
        _init_db()
        idx = sys.argv.index("--cancel")
        try:
            tid = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("python bot.py --cancel <id>")
            return
        ok, msg = _cancel_task(tid)
        print(("✅" if ok else "❌") + f" {msg}")
        return

    if "--reset" in sys.argv:
        _init_db()
        n = _reset_queue()
        print(f"🛑 Сброшено в error: {n}")
        return

    if "--process" in sys.argv:
        _init_db()
        idx = sys.argv.index("--process")
        try:
            url = sys.argv[idx + 1]
        except IndexError:
            print("python bot.py --process <url>")
            return
        info = get_video_info_ytdlp(url)
        ok, reason = await process_and_publish(
            url,
            video_title=info.get("title", ""),
            video_description=info.get("description", ""),
            video_duration=info.get("duration", 0),
            video_published_at=info.get("published_at", ""),
            channel_name=info.get("channel", ""),
            channel_type="media",
            skip_duration_check=True,
        )
        print(f"\n{'=' * 60}")
        print(f"Результат: {'✅ OK' if ok else '❌ ' + reason}")
        print(f"{'=' * 60}")
        await bot.session.close()
        return

    print("🔍 [4/10] перед _init_db")
    _cleanup_temp_files()
    _init_db()
    print("🔍 [5/10] после _init_db")

    if TEST_VIDEO_URL:
        print("\n🧪 ТЕСТ")
        ok, reason = await process_and_publish(TEST_VIDEO_URL, video_title=TEST_VIDEO_TITLE)
        print(f"Тест: {'✅ OK' if ok else '❌ ' + reason}")

    print("🔍 [6/10] перед check_all_channels")
    await check_all_channels(_SHUTDOWN_EVENT)
    print("🔍 [7/10] после check_all_channels")

    print("🔍 [8/10] создаю воркер и планировщик")
    worker_task = asyncio.create_task(_worker_loop(_SHUTDOWN_EVENT))
    scheduler = build_scheduler(bot, _SHUTDOWN_EVENT)
    scheduler.start()
    print("🔍 [9/10] scheduler запущен")

    print(f"\n{'=' * 60}")
    print("🚀 БОТ ГОТОВ")
    print(f"   Воркер: запущен")
    print(f"   Планировщик: каждые {CHECK_INTERVAL_HOURS} ч")
    print(f"   Глубина: {HOURS_BACK} ч")
    print(f"   Whisper: {WHISPER_MODE} ({GROQ_WHISPER_MODEL})")
    print(f"   Модель LLM: {GROQ_MODEL}")
    print(f"   Owner: {OWNER_USER_ID}")
    print(f"   БД: {DB_PATH}")
    print(f"{'=' * 60}\n")

    print("🔍 [10/10] запускаю dp.start_polling()")
    try:
        await dp.start_polling(bot)
    finally:
        print("\n⏹ Останавливаюсь...")
        _SHUTDOWN_EVENT.set()
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        print("⏳ Ожидание завершения текущей задачи (до 10 мин)...")
        try:
            await asyncio.wait_for(worker_task, timeout=600)
            print("✅ Воркер завершён")
        except (asyncio.TimeoutError, asyncio.CancelledError):
            print("⏱ Таймаут — отменяю воркер")
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception as e:
            print(f"⚠️  Ошибка при остановке: {e}")
        try:
            await bot.session.close()
        except Exception:
            pass
        print("⏹ Завершено")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n\n⏹ Остановлено (Ctrl+C)")
    except BaseException as e:
        print(f"\n\n❌ КРИТИЧЕСКАЯ ОШИБКА: {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            sys.stderr.flush()
        except Exception:
            pass