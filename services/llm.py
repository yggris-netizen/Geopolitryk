"""Единый Groq-клиент. Импортируется другими сервисами."""

from groq import Groq
from config import GROQ_API_KEY

# timeout=60s: не висеть бесконечно при сетевых проблемах.
# max_retries=0: свои ретраи уже реализованы в summarization._call_groq_with_retry,
# двойные ретраи только запутывают логи и увеличивают задержки.
groq_client = Groq(api_key=GROQ_API_KEY, timeout=60.0, max_retries=0)