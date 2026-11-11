# -*- coding: utf-8 -*-
"""Определение окружения: pc / laptop / server.

Переменные окружения (в .env):
- BOT_ENV: "pc" | "laptop" | "server" (по умолчанию "pc")
- PROXY_URL: URL прокси для Groq/Telegram (если нужен; для сервера — пусто)
- DB_PATH: путь к БД (по умолчанию — платформозависимый)

Заглушки на будущее:
- IS_SERVER: флаг сервера (для веток кода, где VPN не нужен)
- IS_WINDOWS / IS_LINUX: платформа
"""

import os
import platform
import sys
from pathlib import Path

# ─── Платформа ───
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# ─── Окружение (pc / laptop / server) ───
BOT_ENV = os.getenv("BOT_ENV", "pc").lower()
if BOT_ENV not in ("pc", "laptop", "server"):
    print(f"[WARN]  BOT_ENV={BOT_ENV!r} — неизвестно, использую 'pc'")
    BOT_ENV = "pc"

IS_SERVER = BOT_ENV == "server"
IS_PC = BOT_ENV == "pc"
IS_LAPTOP = BOT_ENV == "laptop"

# ─── Пути ───
if IS_SERVER:
    # Сервер (Ubuntu, /app/) — стандартный путь для Docker/Bothost
    DEFAULT_DB = "/app/data/seen_videos.db"
    DEFAULT_COOKIES = "/app/cookies.txt"
    DEFAULT_SA = "/app/service_account.json"
elif IS_LINUX:
    # Ноутбук (Ubuntu) — домашняя папка
    DEFAULT_DB = str(Path.home() / "News_agent" / "seen_videos.db")
    DEFAULT_COOKIES = str(Path.home() / "News_agent" / "cookies.txt")
    DEFAULT_SA = str(Path.home() / "News_agent" / "service_account.json")
else:
    # ПК (Windows) — локальная папка
    DEFAULT_DB = "seen_videos.db"
    DEFAULT_COOKIES = "cookies.txt"
    DEFAULT_SA = "service_account.json"

DB_PATH = os.getenv("DB_PATH", DEFAULT_DB)
COOKIES_PATH = os.getenv("COOKIES_PATH", DEFAULT_COOKIES)
SERVICE_ACCOUNT_PATH = os.getenv("SERVICE_ACCOUNT_PATH", DEFAULT_SA)

# ─── Прокси (для Groq/Telegram через VPN) ───
# ПК / ноутбук: PROXY_URL задан (например, "socks5://127.0.0.1:1080" для AmnesiaWG)
# Сервер: PROXY_URL не задан (немецкий IP)
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

# ─── Groq API base (если нужен кастомный, например, прокси) ───
GROQ_API_BASE = os.getenv("GROQ_API_BASE", "").strip() or None

# ─── Логирование ───
def print_environment():
    """Печатает текущее окружение (для диагностики)."""
    print(f"[ENV] BOT_ENV: {BOT_ENV}")
    print(f"   IS_WINDOWS: {IS_WINDOWS}, IS_LINUX: {IS_LINUX}")
    print(f"   IS_SERVER: {IS_SERVER}, IS_PC: {IS_PC}, IS_LAPTOP: {IS_LAPTOP}")
    print(f"   DB_PATH: {DB_PATH}")
    print(f"   COOKIES_PATH: {COOKIES_PATH}")
    print(f"   SERVICE_ACCOUNT_PATH: {SERVICE_ACCOUNT_PATH}")
    print(f"   PROXY_URL: {PROXY_URL or '(не задан)'}")
    print(f"   GROQ_API_BASE: {GROQ_API_BASE or '(по умолчанию)'}")

if __name__ == "__main__":
    print_environment()
