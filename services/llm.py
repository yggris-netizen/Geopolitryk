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
# Прокси для httpx (если PROXY_URL задан)
if PROXY_URL:
    import httpx
    groq_client._client = httpx.Client(proxy=PROXY_URL, timeout=120.0)
    print(f'🌐 Groq через прокси: {PROXY_URL}')
else:
    print('🌐 Groq напрямую (без прокси)')
