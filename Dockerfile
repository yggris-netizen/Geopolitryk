ARG CACHEBUST=2026-11-01-04
FROM python:3.11-slim

# Системные зависимости: только ffmpeg и curl.
# Node.js больше НЕ нужен — PO Token Provider теперь Rust-бинарник.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Rust-бинарник PO Token Provider (один файл ~15 МБ, без зависимостей)
RUN curl -L -o /usr/local/bin/bgutil-pot \
        https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-x86_64 \
    && chmod +x /usr/local/bin/bgutil-pot

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

CMD ["/app/start.sh"]