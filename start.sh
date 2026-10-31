#!/bin/bash
echo "=== DIAGNOSTICS START ==="
echo "PWD: $(pwd)"
echo "PATH: $PATH"
echo "Python (python): $(which python 2>&1 || echo 'NOT FOUND')"
echo "Python (python3): $(which python3 2>&1 || echo 'NOT FOUND')"
echo "Python version: $(python3 --version 2>&1 || python --version 2>&1 || echo 'ERROR')"
echo "BOT_TOKEN set: $([ -n "$BOT_TOKEN" ] && echo yes || echo no)"
echo "GROQ_API_KEY set: $([ -n "$GROQ_API_KEY" ] && echo yes || echo no)"
echo "CHANNEL_ID: $CHANNEL_ID"
echo "OWNER_USER_ID: $OWNER_USER_ID"
echo "DB_PATH: $DB_PATH"
echo "=== TRYING TO RUN BOT ==="
python3 bot.py 2>&1
echo "=== EXIT CODE: $? ==="
echo "=== DIAGNOSTICS END ==="
sleep 10