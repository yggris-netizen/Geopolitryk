"""Единый Groq-клиент. Импортируется другими сервисами."""

from groq import Groq
from config import GROQ_API_KEY

groq_client = Groq(api_key=GROQ_API_KEY)