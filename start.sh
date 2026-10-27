#!/bin/bash
set -e

echo "Starting PO Token Provider..."
node /opt/bgutil/server/build/main.js --port 4416 &

echo "Waiting for server..."
for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:4416/ping > /dev/null; then
        echo "Token server ready!"
        break
    fi
    sleep 1
done

echo "Starting bot..."
exec python bot.py