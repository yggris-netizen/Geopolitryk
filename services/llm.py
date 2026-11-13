"""Единый Groq-клиент. Импортируется другими сервисами."""

from groq import Groq
from services.environment import GROQ_API_BASE, PROXY_URL
from config import GROQ_API_KEY
from db.token_usage import can_call, log_usage, mark_exhausted

# timeout=60s: не висеть бесконечно при сетевых проблемах.
# max_retries=0: свои ретраи уже реализованы в summarization._call_groq_with_retry,
# двойные ретраи только запутывают логи и увеличивают задержки.
groq_client = Groq(
    api_key=GROQ_API_KEY,
    base_url=GROQ_API_BASE,  # None -> стандартный api.groq.com
    max_retries=0,           # ретраи — только в summarization._call_groq_with_retry
)


# ═══════════════════════════════════════════════════════════
# P1.5: circuit breaker (5 failures -> 60s pause)
# ═══════════════════════════════════════════════════════════
import time as _time

# _logging_silenced: приглушаем шумные библиотеки
import logging as _logging
_logging.getLogger("aiogram").setLevel(_logging.WARNING)
_logging.getLogger("google.api_core").setLevel(_logging.ERROR)
_logging.getLogger("httpx").setLevel(_logging.WARNING)


_CIRCUIT_THRESHOLD = 5
_CIRCUIT_PAUSE_SEC = 60
_consecutive_failures = 0
_last_failure_ts = 0.0

# Ошибки, которые НЕ считаем (429/TPM/TPD — обрабатываются каскадом)
_TRANSIENT_429_MARKERS = ("429", "rate_limit", "rate limit", "tokens per day", "tpd", "tokens per minute", "tpm")


def _is_429(err: str) -> bool:
    e = err.lower()
    return any(m in e for m in _TRANSIENT_429_MARKERS)


def groq_chat_create(task=None, **kwargs):
    """Обёртка: TPD-предохранитель + circuit breaker + log_usage.

    task: 'low' / 'medium' / 'high' / 'map' / 'reduce' — для учёта токенов.
    """
    global _consecutive_failures, _last_failure_ts

    # TPD-предохранитель (до вызова)
    model = kwargs.get("model", "")
    if model and not can_call(model, estimated_tokens=0):
        print(f"[TPD] {model}: лимит исчерпан (>=90%), пропуск")
        raise RuntimeError(f"tpd_exhausted:{model}")

    # Circuit breaker (как было)
    if _consecutive_failures >= _CIRCUIT_THRESHOLD:
        now = _time.time()
        wait = _CIRCUIT_PAUSE_SEC - (now - _last_failure_ts)
        if wait > 0:
            print(f"[CB] Circuit breaker: pause {wait:.0f}с")
            _time.sleep(wait)
        _consecutive_failures = 0

    try:
        r = groq_client.chat.completions.create(**kwargs)
        _consecutive_failures = 0
        try:
            u = getattr(r, "usage", None)
            if u is not None:
                log_usage(
                    model=model, task=task or "unknown",
                    prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                    total_tokens=getattr(u, "total_tokens", 0) or 0,
                )
        except Exception as e:
            print(f"[TPD] usage log: {e!r}")
        return r
    except Exception as e:
        err_low = str(e).lower()
        if ("tokens per day" in err_low or "tpd" in err_low) and model:
            try:
                mark_exhausted(model, task or "unknown")
            except Exception:
                pass
        if not _is_429(str(e)):
            _consecutive_failures += 1
            _last_failure_ts = _time.time()
        raise

# Прокси для httpx (если PROXY_URL задан)
if PROXY_URL:
    import httpx
    groq_client._client = httpx.Client(proxy=PROXY_URL, timeout=120.0)
    print(f'[NET] Groq via proxy: {PROXY_URL}')
else:
    print('[NET] Groq direct (no proxy)')
