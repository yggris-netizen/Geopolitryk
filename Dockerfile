ARG CACHEBUST=2026-11-01-06
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    file \
    && rm -rf /var/lib/apt/lists/*

# Rust-бинарник PO Token Provider — с fail-fast и проверкой
RUN curl -fL -o /usr/local/bin/bgutil-pot \
        "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-x86_64" \
    && chmod +x /usr/local/bin/bgutil-pot \
    && file /usr/local/bin/bgutil-pot \
    && /usr/local/bin/bgutil-pot --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

# ВАЖНО: запускаем через bash, а не напрямую.
# Bothost монтирует /app из Git, и бит +x у start.sh может потеряться.
CMD ["bash", "/app/start.sh"]