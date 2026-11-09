"""
Централизованная конфигурация бота.
Все константы, env-переменные, классификация ошибок.
Версия: v2.1.7-local (07.11.2026)
- 7 каналов (убраны MayiakUA, Свободный, Легіст — неинформативны)
- MAX_VIDEO_DURATION_SECONDS = 7200 (2 часа)
- NOTIFY_UNKNOWN_VOICES = False (уведомления отключены, pending — всегда)"""

# ─── P1.2: магические числа (вынесены из кода) ───────────────
# expert_detection.py — negative check
NEGATIVE_COS_THRESHOLD = 0.90
NEGATIVE_DIFF_THRESHOLD = 0.03

# quote_verifier.py — лимит цитат
MAX_QUOTES = 999

# summary_editor.py — D2: минимум символов (иначе fallback D1)
D2_MIN_CHARS = 200

# telegram.py — обрезка в публикации
QUOTE_PREVIEW_LEN = 200
ERROR_PREVIEW_LEN = 200


import json
import os
import sys
import atexit
import tempfile

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from services.environment import (
    BOT_ENV, IS_SERVER, IS_WINDOWS, IS_LINUX,
    DB_PATH as _ENV_DB_PATH,
    SERVICE_ACCOUNT_PATH as _ENV_SA_PATH,
    PROXY_URL, GROQ_API_BASE,
)


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "0")
try:
    CHANNEL_ID = int(CHANNEL_ID_RAW)
except ValueError:
    print(f"❌ CHANNEL_ID={CHANNEL_ID_RAW!r} — не число. Проверьте .env")
    sys.exit(1)
OWNER_USER_ID_RAW = os.getenv("OWNER_USER_ID", "0")
try:
    OWNER_USER_ID = int(OWNER_USER_ID_RAW)
except ValueError:
    print(f"❌ OWNER_USER_ID={OWNER_USER_ID_RAW!r} — не число. Проверьте .env")
    sys.exit(1)

WHISPER_MODE = os.getenv("WHISPER_MODE", "groq")
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-120b")

# Дополнительные модели для каскада (free tier: +200K +300K TPD)
GROQ_EXTRA_MODELS = [
    "qwen/qwen3.8-27b",
]

# Маршрутизация задач по моделям (бесплатные)
# low    — простые (нейтрализация, хештеги) -> 20b
# medium — средние (извлечение цитат) -> 120b
# high   — сложные (прогнозы, аналитика) -> kimi
GROQ_TASK_MODELS = {
    "low":    ["openai/gpt-oss-20b", "openai/gpt-oss-120b"],
    "medium": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
    "high":   ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"],
}



MAX_TASK_ATTEMPTS = int(os.getenv("MAX_TASK_ATTEMPTS", "5"))
DIAGNOSTIC_MODE = os.getenv("DIAGNOSTIC_MODE", "0") == "1"
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "tiny")
GROQ_MAX_CHUNK_SECONDS = int(os.getenv("GROQ_MAX_CHUNK_SECONDS", "300"))
HEARTBEAT_HOURS = int(os.getenv("HEARTBEAT_HOURS", "0"))

GROQ_MAX_SIZE_MB = 24.0
TELEGRAM_MAX_LEN = 4096

# Tavily (Bot 3, для scripts/test_factcheck.py)
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# Уведомления о незнакомых голосах (pending сохраняется всегда, уведомление — по флагу)
NOTIFY_UNKNOWN_VOICES = os.getenv("NOTIFY_UNKNOWN_VOICES", "0") == "1"


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
    # P0.2: удалить tempfile при выходе (утечка секретов)
    def _cleanup_temp_creds():
        try:
            if os.path.exists(SERVICE_ACCOUNT_FILE):
                os.unlink(SERVICE_ACCOUNT_FILE)
                print(f"🧹 Удалён tempfile: {SERVICE_ACCOUNT_FILE}")
        except Exception as e:
            print(f"⚠️  tempfile cleanup: {e}")
    atexit.register(_cleanup_temp_creds)
else:
    SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", _ENV_SA_PATH)


YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

# 7 каналов (v2.1.7: убраны MayiakUA, Свободный, Легіст)
CHANNELS_TO_WATCH = [
    {"id": "UCSRtRl2Msw71NnYQduHmMgw", "name": "Александр Шелест"},
    {"id": "UCT0ve_Yq3PPSjw3pJDOK5DA", "name": "Руслан Бортник"},
    {"id": "UCe3crzYhR7bx4sTY-83H2Gg", "name": "Дикий live"},
    {"id": "UCdGKO6B6OWbEtWRk6bx4HkA", "name": "КАТАРСИС"},
    {"id": "UCHgiqmsIHN2PlQnBIRFRGGg", "name": "Politeka Online"},
    {"id": "UCxkdtxa54kQLRmmWaBH0K2w", "name": "UKRLIFETV"},
    {"id": "UC8CAQSzI8OdvubZi_PR6JKg", "name": "PolitExpert"},
]

TEST_VIDEO_URL = ""
TEST_VIDEO_TITLE = ""

VIDEO_LANGUAGE = "ru"
DB_PATH = os.getenv("DB_PATH", _ENV_DB_PATH)

CHECK_INTERVAL_HOURS = 3
WORKER_POLL_INTERVAL = 5

HOURS_BACK = int(os.getenv("HOURS_BACK", "24"))
DAYS_BACK = HOURS_BACK / 24

MIN_VIDEO_DURATION_SECONDS = 180
MAX_VIDEO_DURATION_SECONDS = 7200   # 2 часа
MAX_VIDEOS_PER_CHANNEL_PER_CHECK = int(os.getenv("MAX_VIDEOS_PER_CHANNEL_PER_CHECK", "5"))
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

SUMMARIZE_CHUNK_CHARS = int(os.getenv("SUMMARIZE_CHUNK_CHARS", "5000"))
SUMMARIZE_FINAL_TOKENS = int(os.getenv("SUMMARIZE_FINAL_TOKENS", "3000"))
SUMMARIZE_PARTIAL_TOKENS = int(os.getenv("SUMMARIZE_PARTIAL_TOKENS", "4000"))

GROQ_DELAY_BETWEEN_CHUNKS = int(os.getenv("GROQ_DELAY_BETWEEN_CHUNKS", "20"))  # P1.4: 45->20 (TPM 8000)
GROQ_PREDICTIONS_PAUSE = int(os.getenv("GROQ_PREDICTIONS_PAUSE", "10"))  # P1.4: 45->10
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "5"))

DISPLAY_TIMEZONE_OFFSET = 3

TRANSCRIPTS_CACHE_DAYS = int(os.getenv("TRANSCRIPTS_CACHE_DAYS", "7"))


# ─── Диаризация (SpeechBrain ECAPA) ────────────────────────
HF_TOKEN = os.getenv("HF_TOKEN", "")
DIARIZATION_ENABLED = os.getenv("DIARIZATION_ENABLED", "1") == "1"
DIARIZATION_MIN_SPEAKERS = int(os.getenv("DIARIZATION_MIN_SPEAKERS", "1"))
DIARIZATION_MAX_SPEAKERS = int(os.getenv("DIARIZATION_MAX_SPEAKERS", "4"))


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
    "cookies_expired",
)

_TRANSIENT_ERROR_MARKERS = (
    "429", "rate_limit", "rate limit", "quotaexceeded",
    "timeout", "timed out", "connectionerror", "connection error",
    "connection reset", "remotedisconnected",
    "service unavailable", "bad gateway",
    " 500", " 502", " 503", " 504",
    "internal server error",
    "tokens per day",
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