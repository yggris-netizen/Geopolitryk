# -*- coding: utf-8 -*-
"""Передача аудио через rclone (SFTP). Resume при обрыве — из коробки."""
import json
import os
import subprocess

from config import RCLONE_PATH, RCLONE_REMOTE

_TIMEOUT = 600


def _run_rclone(args: list) -> subprocess.CompletedProcess:
    if not RCLONE_PATH or not os.path.exists(RCLONE_PATH):
        raise RuntimeError(f"rclone.exe не найден: {RCLONE_PATH!r}. Проверьте RCLONE_PATH в .env")
    if not RCLONE_REMOTE:
        raise RuntimeError("RCLONE_REMOTE не задан в .env (пример: server:/app/audio/)")
    cmd = [
        RCLONE_PATH, *args,
        "--retries", "3",
        "--low-level-retries", "10",
        "--stats", "10s",
        "--stats-one-line",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT, check=True)


def upload_audio(local_path: str, video_id: str) -> str:
    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)
    remote_name = f"{video_id}.mp3"
    print(f"[RCLONE] upload: {local_path} -> {RCLONE_REMOTE}{remote_name}")
    r = _run_rclone(["copyto", local_path, f"{RCLONE_REMOTE}{remote_name}"])
    print(f"[RCLONE] upload OK: {r.stdout.strip()[:200]}")
    return f"{RCLONE_REMOTE}{remote_name}"


def download_audio(remote_name: str, local_path: str) -> bool:
    print(f"[RCLONE] download: {RCLONE_REMOTE}{remote_name} -> {local_path}")
    r = _run_rclone(["copyto", f"{RCLONE_REMOTE}{remote_name}", local_path])
    print(f"[RCLONE] download OK: {r.stdout.strip()[:200]}")
    return os.path.exists(local_path)


def verify_size(local_path: str, remote_name: str) -> bool:
    if not os.path.exists(local_path):
        print(f"[RCLONE] verify: локальный файл нет: {local_path}")
        return False
    try:
        r = _run_rclone(["size", f"{RCLONE_REMOTE}{remote_name}", "--json"])
        data = json.loads(r.stdout.strip() or "{}")
        remote_bytes = int(data.get("bytes", 0))
        local_bytes = os.path.getsize(local_path)
        ok = (remote_bytes == local_bytes)
        print(f"[RCLONE] verify: local={local_bytes} remote={remote_bytes} -> {'OK' if ok else 'MISMATCH'}")
        return ok
    except Exception as e:
        print(f"[RCLONE] verify error: {e!r}")
        return False


def remote_exists(remote_name: str) -> bool:
    try:
        r = _run_rclone(["lsf", f"{RCLONE_REMOTE}{remote_name}"])
        return bool((r.stdout or "").strip())
    except Exception:
        return False
