"""
Централизованная конфигурация бота.
Все константы, env-переменные, классификация ошибок.
Не зависит от aiogram/groq — только stdlib.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ╔══════════════════════════════════════════════════════════╗
# ║                    НАСТРОЙКИ                             ║
# ╚══════════════════════════════════════════════════════════╝

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))

WHISPER_MODE = os.getenv("WHISPER_MODE", "groq")
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

MAX_TASK_ATTEMPTS = int(os.getenv("MAX_TASK_ATTEMPTS", "5"))
DIAGNOSTIC_MODE = os.getenv("DIAGNOSTIC_MODE", "0") == "1"
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "tiny")
GROQ_MAX_CHUNK_SECONDS = int(os.getenv("GROQ_MAX_CHUNK_SECONDS", "300"))
HEARTBEAT_HOURS = int(os.getenv("HEARTBEAT_HOURS", "0"))

GROQ_MAX_SIZE_MB = 24.0
TELEGRAM_MAX_LEN = 4096


# ─── Google Service Account ────────────────────────────────
_creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if _creds_json:
    _creds_json = _creds_json.strip()
    if len(_creds_json) >= 2 and _creds_json[0] == '"' and _creds_json[-1] == '"':
        _creds_json = _creds_json[1:-1]
    _creds_json = _creds_json.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    try:
        _parsed_creds = json.loads(_creds_json)
        if not isinstance(_parsed_creds, dict) or "type" not in _parsed_creds:
            raise ValueError("JSON не содержит поля 'type'")
    except json.JSONDecodeError as _e:
        print(f"❌ GOOGLE_CREDENTIALS_JSON невалиден: {_e}")
        print(f"   Первые 80 символов: {_creds_json[:80]!r}")
        sys.exit(1)
    _creds_json = json.dumps(_parsed_creds, ensure_ascii=False)
    _creds_tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    _creds_tmp.write(_creds_json)
    _creds_tmp.close()
    SERVICE_ACCOUNT_FILE = _creds_tmp.name
else:
    SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")


YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

CHANNELS_TO_WATCH = [
    {"id": "UCSRtRl2Msw71NnYQduHmMgw", "name": "Александр Шелест", "type": "author", "owner": "Александр Шелест"},
    {"id": "UCT0ve_Yq3PPSjw3pJDOK5DA", "name": "Руслан Бортник",   "type": "author", "owner": "Руслан Бортник"},
    {"id": "UCe3crzYhR7bx4sTY-83H2Gg", "name": "Дикий live",       "type": "author", "owner": "Виталий Дикий"},
    {"id": "UCdGKO6B6OWbEtWRk6bx4HkA", "name": "КАТАРСИС",         "type": "media"},
    {"id": "UCHgiqmsIHN2PlQnBIRFRGGg", "name": "Politeka Online",  "type": "media"},
    {"id": "UCxkdtxa54kQLRmmWaBH0K2w", "name": "UKRLIFETV",        "type": "media"},
]

TEST_VIDEO_URL = ""
TEST_VIDEO_TITLE = ""

VIDEO_LANGUAGE = "ru"
DB_PATH = os.getenv("DB_PATH", "seen_videos.db")

CHECK_INTERVAL_HOURS = 3
WORKER_POLL_INTERVAL = 5

HOURS_BACK = int(os.getenv("HOURS_BACK", "12"))
DAYS_BACK = HOURS_BACK / 24

MIN_VIDEO_DURATION_SECONDS = 180
MAX_VIDEO_DURATION_SECONDS = 5400
MAX_VIDEOS_PER_CHANNEL_PER_CHECK = int(os.getenv("MAX_VIDEOS_PER_CHANNEL_PER_CHECK", "5"))
MAX_REPEAT_EXPERTS_PER_CYCLE = 2
MIN_TRANSCRIPT_CHARS = 100

YOUTUBE_VIDEOS_CACHE_TTL = 1800
YOUTUBE_DURATIONS_CACHE_TTL = 86400
YOUTUBE_CACHE_MAX_ENTRIES = 1000

YOUTUBE_DAILY_QUOTA = 10000

NEUTRALIZE_TITLE = True
MAX_TITLE_LEN = 90
GENERATE_HASHTAGS = True

EXTRACT_PREDICTIONS = True
MAX_PREDICTIONS_PER_VIDEO = 10
MAX_PREDICTION_HORIZON_YEARS = 5

GROQ_DELAY_BETWEEN_CHUNKS = 5
GROQ_MAX_RETRIES = 5

DISPLAY_TIMEZONE_OFFSET = 3


# ╔══════════════════════════════════════════════════════════╗
# ║           КЛАССИФИКАЦИЯ ОШИБОК                            ║
# ╚══════════════════════════════════════════════════════════╝

_FATAL_ERROR_MARKERS = (
    "too_short:", "too_long:", "transcript_too_short:",
    "ffmpeg_split_failed", "chunk_too_big",
    "video unavailable", "private video", "deleted video",
    "http error 404", "http error 410",
    "message is too long",
    "chat not found", "bot was blocked",
)

_TRANSIENT_ERROR_MARKERS = (
    "429", "rate_limit", "rate limit", "quotaexceeded",
    "timeout", "timed out", "connectionerror", "connection error",
    "connection reset", "remotedisconnected",
    "service unavailable", "bad gateway",
    " 500", " 502", " 503", " 504",
    "internal server error",
)


def _classify_error(reason: str) -> str:
    if not reason:
        return "unknown"
    r = reason.lower()
    for m in _FATAL_ERROR_MARKERS:
        if m in r:
            return "fatal"
    for m in _TRANSIENT_ERROR_MARKERS:
        if m in r:
            return "transient"
    return "unknown"