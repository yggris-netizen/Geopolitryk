# -*- coding: utf-8 -*-
"""GigaChat API: менеджер access_token + chat/completions (fallback для Groq).

Authorization Key (config.GIGACHAT_AUTH_KEY) обменивается на access_token
(живёт 30 мин), кэшируется, авто-обновляется за 60с до истечения.
"""
import os
import time
import uuid
import threading

import httpx
import ssl

from config import (
    GIGACHAT_AUTH_KEY,
    GIGACHAT_MODEL_LOW,
    GIGACHAT_MODEL_HIGH,
    USE_GIGACHAT,
)

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_BASE = "https://api.giga.chat/v1"
SCOPE = "GIGACHAT_API_PERS"

# Явный SSL-контекст: системные корни + сертификат Минцифры (НУЦ).
_CRT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "russian_trusted_root_ca_pem.crt",
)
_ssl_ctx = ssl.create_default_context()
if os.path.exists(_CRT_PATH):
    _ssl_ctx.load_verify_locations(cafile=_CRT_PATH)

_token_lock = threading.Lock()
_access_token = None
_expires_at = 0.0


def _get_access_token() -> str:
    """Обмен Authorization Key -> access_token (кэш 30 мин, обновление за 60с)."""
    global _access_token, _expires_at
    with _token_lock:
        now = time.time()
        if _access_token and now < _expires_at - 60:
            return _access_token
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
        }
        r = httpx.post(OAUTH_URL, headers=headers,
                       data={"scope": SCOPE}, timeout=30.0,
                       verify=_ssl_ctx)
        r.raise_for_status()
        data = r.json()
        _access_token = data["access_token"]
        # expires_at — unix ms (в некоторых ответах — сек)
        exp = data.get("expires_at", 0)
        if exp > 10_000_000_000:
            _expires_at = exp / 1000.0
        else:
            _expires_at = float(exp) if exp else (now + 1800)
        return _access_token


def gigachat_chat_create(task: str = "low", messages=None,
                         max_tokens: int = 2000, temperature: float = 0.2):
    """Chat/completions через GigaChat. Возвращает content (str)."""
    if not USE_GIGACHAT:
        raise RuntimeError("gigachat_disabled")
    if not GIGACHAT_AUTH_KEY:
        raise RuntimeError("gigachat_no_auth_key")
    model = GIGACHAT_MODEL_LOW if task in ("low", "medium") else GIGACHAT_MODEL_HIGH
    token = _get_access_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "model": model,
        "messages": messages or [],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    r = httpx.post(f"{API_BASE}/chat/completions", headers=headers,
                   json=payload, timeout=120.0,
                   verify=_ssl_ctx)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()
