#!/bin/bash
set -e

echo "=== RUST PO TOKEN DIAGNOSTICS ==="
echo "file /usr/local/bin/bgutil-pot:"
file /usr/local/bin/bgutil-pot 2>&1 || echo "FILE NOT FOUND"
echo "ls -la /usr/local/bin/bgutil-pot:"
ls -la /usr/local/bin/bgutil-pot 2>&1 || echo "LS FAILED"
echo "=== STARTING SERVER ==="

echo "Starting PO Token Provider (Rust)..."
bgutil-pot server --host 127.0.0.1 --port 4416 &
POT_PID=$!

echo "Waiting for token server (pid=$POT_PID)..."
for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:4416/ping > /dev/null; then
        echo "Token server ready!"
        break
    fi
    if ! kill -0 $POT_PID 2>/dev/null; then
        echo "❌ PO Token server died (pid=$POT_PID)"
        break
    fi
    sleep 1
done

echo "Starting bot..."
exec python3 -u bot.py