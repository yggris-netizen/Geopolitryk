# -*- coding: utf-8 -*-
"""Локальная LLM через Ollama (fallback для MAP, 0 TPD).

Saiga-Llama3-8B (основная) -> RusMind-Zernyshko-2.0 (запасная).
API: POST {OLLAMA_URL}/api/chat, stream=false.
Возвращает объект с .choices[0].message.content (совместим с Groq).
"""
import httpx

from config import (
    USE_LOCAL_LLM, OLLAMA_URL, OLLAMA_MODEL, OLLAMA_MODEL_FALLBACK,
)


class _Msg:
    def __init__(self, content: str):
        self.content = content


class _Choice:
    def __init__(self, content: str):
        self.message = _Msg(content)
        self.finish_reason = "stop"


class _Response:
    def __init__(self, content: str, usage: dict | None = None):
        self.choices = [_Choice(content)]
        self.usage = usage or {}


def _call_ollama(model: str, messages: list, max_tokens: int,
                 temperature: float) -> _Response:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "top_k": 30,
            "repeat_penalty": 1.12,
        },
    }
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300.0)
    r.raise_for_status()
    data = r.json()
    content = ((data.get("message") or {}).get("content") or "").strip()
    usage = {
        "prompt_tokens": data.get("prompt_eval_count", 0),
        "completion_tokens": data.get("eval_count", 0),
        "total_tokens": (data.get("prompt_eval_count", 0)
                         + data.get("eval_count", 0)),
    }
    return _Response(content, usage)


_TASK_SYSTEM = {
    "low": ("Ты - Сайга, русскоязычный ассистент. "
            "Отвечай ТОЛЬКО JSON, без markdown и пояснений."),
    "medium": ("Ты - Сайга, русскоязычный ассистент. "
               "Пиши связный текст, 4-5 абзацев. "
               "Не добавляй своих фактов."),
    "high": ("Ты - Сайга, русскоязычный ассистент. "
             "Верни ТОЛЬКО JSON-массив. Если прогнозов нет - []."),
}
_TASK_TEMP = {"low": 0.1, "medium": 0.4, "high": 0.1}
_TASK_MAX = {"low": 3000, "medium": 5000, "high": 2000}


def local_chat_create(task: str = "low", messages: list = None,
                      max_tokens: int = None,
                      temperature: float = None) -> _Response:
    """Ollama chat с режимом под задачу (low/medium/high)."""
    if not USE_LOCAL_LLM:
        raise RuntimeError("local_llm_disabled")
    task = task or "low"
    if temperature is None:
        temperature = _TASK_TEMP.get(task, 0.2)
    if max_tokens is None:
        max_tokens = _TASK_MAX.get(task, 3000)
    sys_prompt = _TASK_SYSTEM.get(task)
    msgs = list(messages or [])
    if sys_prompt:
        msgs = [{"role": "system", "content": sys_prompt}] + msgs
    try:
        return _call_ollama(OLLAMA_MODEL, msgs, max_tokens, temperature)
    except Exception as e:
        print(f"[LOCAL] {OLLAMA_MODEL} failed: {e!r} -> fallback")
        return _call_ollama(OLLAMA_MODEL_FALLBACK, msgs,
                            max_tokens, temperature)
