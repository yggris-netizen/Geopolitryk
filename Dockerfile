FROM python:3.11-slim

# Системные зависимости: ffmpeg для аудио, nodejs/npm для сервера токенов, git для клонирования
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    git \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-зависимости (включая плагин bgutil)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Клонируем и собираем сервер PO Token
RUN git clone --single-branch --branch 2.0.2 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil
WORKDIR /opt/bgutil/server
RUN npm ci && npx tsc

# Возвращаемся в /app, копируем код бота и скрипт запуска
WORKDIR /app
COPY . .
RUN chmod +x start.sh

CMD ["/app/start.sh"]