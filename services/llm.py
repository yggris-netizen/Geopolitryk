"""Единый Groq-клиент. Импортируется другими сервисами."""

from groq import Groq
from services.environment import GROQ_API_BASE, PROXY_URL
from config import GROQ_API_KEY

# timeout=60s: не висеть бесконечно при сетевых проблемах.
# max_retries=0: свои ретраи уже реализованы в summarization._call_groq_with_retry,
# двойные ретраи только запутывают логи и увеличивают задержки.
groq_client = Groq(
    api_key=GROQ_API_KEY,
    base_url=GROQ_API_BASE,  # None -> стандартный api.groq.com
)


# ═══════════════════════════════════════════════════════════
# P1.5: circuit breaker (5 failures -> 60s pause)
# ═══════════════════════════════════════════════════════════
import time as _time

_CIRCUIT_THRESHOLD = 5
_CIRCUIT_PAUSE_SEC = 60
_consecutive_failures = 0
_last_failure_ts = 0.0

# Ошибки, которые НЕ считаем (429/TPM/TPD — обрабатываются каскадом)
_TRANSIENT_429_MARKERS = ("429", "rate_limit", "rate limit", "tokens per day", "tpd", "tokens per minute", "tpm")


def _is_429(err: str) -> bool:
    e = err.lower()
    return any(m in e for m in _TRANSIENT_429_MARKERS)


def groq_chat_create(**kwargs):
    """Обёртка над groq_client.chat.completions.create с circuit breaker.

    Считает consecutive failures (кроме 429). При >= 5 — пауза 60с.
    """
    global _consecutive_failures, _last_failure_ts
    if _consecutive_failures >= _CIRCUIT_THRESHOLD:
        now = _time.time()
        wait = _CIRCUIT_PAUSE_SEC - (now - _last_failure_ts)
        if wait > 0:
            print(f"🔌 Circuit breaker: пауза {wait:.0f}с (Groq падал {_consecutive_failures} раз)")
            _time.sleep(wait)
        _consecutive_failures = 0
    try:
        r = groq_client.chat.completions.create(**kwargs)
        _consecutive_failures = 0
        return r
    except Exception as e:
        if not _is_429(str(e)):
            _consecutive_failures += 1
            _last_failure_ts = _time.time()
        raise
# Прокси для httpx (если PROXY_URL задан)
if PROXY_URL:
    import httpx
    groq_client._client = httpx.Client(proxy=PROXY_URL, timeout=120.0)
    print(f'🌐 Groq через прокси: {PROXY_URL}')
else:
    print('🌐 Groq напрямую (без прокси)')
