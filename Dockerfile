ARG CACHEBUST=2026-11-01-07
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    file \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем Deno (JavaScript runtime для yt-dlp)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

# Rust-бинарник PO Token Provider
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

CMD ["bash", "/app/start.sh"]