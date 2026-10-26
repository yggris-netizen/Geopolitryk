"""Скачивание аудио и транскрибация (Groq Whisper или локально)."""

import os
import subprocess

import yt_dlp

from config import (
    GROQ_MAX_CHUNK_SECONDS, GROQ_MAX_SIZE_MB, GROQ_WHISPER_MODEL,
    VIDEO_LANGUAGE, WHISPER_MODE, WHISPER_MODEL_SIZE,
)
from services.llm import groq_client

_local_model = None
if WHISPER_MODE == "local":
    print(f"\nЗагружаю локальный Whisper '{WHISPER_MODEL_SIZE}'...")
    try:
        from faster_whisper import WhisperModel
        _local_model = WhisperModel(
            WHISPER_MODEL_SIZE, device="cpu", compute_type="int8",
            cpu_threads=1, num_workers=1,
        )
        print("✅ Whisper OK")
    except Exception as _e:
        print(f"❌ Whisper: {_e}")
        raise
else:
    print("✅ Groq Whisper API (без локальной модели)")


def _split_audio(src: str, chunk_seconds: int) -> list:
    base, _ = os.path.splitext(src)
    chunks = []
    idx = 0
    start = 0
    while True:
        out = f"{base}.part{idx}.m4a"
        cmd = [
            "ffmpeg", "-y", "-ss", str(start), "-i", src,
            "-t", str(chunk_seconds), "-vn", "-c:a", "aac", "-b:a", "64k", out,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        except FileNotFoundError:
            print("   ❌ ffmpeg НЕ НАЙДЕН в PATH.")
            break
        except subprocess.CalledProcessError as e:
            err_tail = (e.stderr or b"")[-300:].decode("utf-8", errors="ignore")
            print(f"   ⚠️  ffmpeg split (start={start}): {err_tail}")
            break
        except Exception as e:
            print(f"   ⚠️  ffmpeg split: {e}")
            break
        if not os.path.exists(out) or os.path.getsize(out) < 1024:
            if os.path.exists(out):
                try:
                    os.remove(out)
                except Exception:
                    pass
            break
        chunks.append(out)
        start += chunk_seconds
        idx += 1
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", src],
                check=True, capture_output=True, timeout=10, text=True,
            )
            total = float(probe.stdout.strip())
            if start >= total:
                break
        except Exception:
            break
    return chunks


def download_audio(url: str, output_path: str = "audio.mp3") -> str:
    base = os.path.splitext(output_path)[0]
    for ext in (".mp3", ".m4a", ".webm", ".opus", ".mp4", ".ogg", ".wav", ".aac", ".flac"):
        for suffix in ("", ".part"):
            f = base + ext + suffix
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    ydl_opts = {
        'format': 'bestaudio[abr<=96]/bestaudio/best',
        'outtmpl': base + ".%(ext)s",
        'quiet': False,
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '64',
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_path


def _transcribe_groq(audio_path: str, total_duration: float = 0) -> str:
    file_size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"   📁 Размер: {file_size_mb:.1f} MB")

    chunks = []
    if file_size_mb > GROQ_MAX_SIZE_MB:
        print(f"   ✂️  нарезаю по {GROQ_MAX_CHUNK_SECONDS}с...")
        chunks = _split_audio(audio_path, GROQ_MAX_CHUNK_SECONDS)
        if not chunks:
            raise RuntimeError(
                f"ffmpeg_split_failed: файл {file_size_mb:.1f} MB > "
                f"{GROQ_MAX_SIZE_MB} MB, нарезка не удалась."
            )
        for chunk in chunks:
            size_mb = os.path.getsize(chunk) / 1024 / 1024
            if size_mb > GROQ_MAX_SIZE_MB:
                raise RuntimeError(
                    f"chunk_too_big: {size_mb:.1f} MB > {GROQ_MAX_SIZE_MB} MB "
                    f"({os.path.basename(chunk)})."
                )
    else:
        chunks = [audio_path]

    print(f"   📤 Отправляю {len(chunks)} кусков в Groq...")
    full_parts = []
    for i, chunk in enumerate(chunks, 1):
        chunk_mb = os.path.getsize(chunk) / 1024 / 1024
        print(f"   [{i}/{len(chunks)}] {chunk_mb:.1f} MB...")
        try:
            with open(chunk, "rb") as f:
                r = groq_client.audio.transcriptions.create(
                    file=(os.path.basename(chunk), f),
                    model=GROQ_WHISPER_MODEL,
                    language=VIDEO_LANGUAGE,
                    response_format="text",
                )
            text = (r.text if hasattr(r, "text") else str(r)).strip()
            print(f"   ✅ {len(text)} символов")
            full_parts.append(text)
        except Exception as e:
            print(f"   ❌ Groq chunk {i}: {e}")
            raise
        finally:
            if chunk != audio_path and os.path.exists(chunk):
                try:
                    os.remove(chunk)
                except Exception:
                    pass

    full = " ".join(full_parts)
    if os.path.exists(audio_path):
        try:
            os.remove(audio_path)
        except Exception:
            pass
    return full


def _transcribe_local(audio_path: str, total_duration: float = 0) -> str:
    if _local_model is None:
        raise RuntimeError("локальный Whisper не инициализирован")
    segments, _info = _local_model.transcribe(
        audio_path, beam_size=5, language=VIDEO_LANGUAGE, vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=("Это интервью на русском языке. Обсуждается геополитика, "
                        "политика, экономика, выборы, парламент, эксперты."),
    )
    parts = []
    for seg in segments:
        parts.append(seg.text.strip())
    full = " ".join(parts)
    if os.path.exists(audio_path):
        try:
            os.remove(audio_path)
        except Exception:
            pass
    return full


def transcribe_audio(audio_path: str, total_duration: float = 0) -> str:
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)
    if WHISPER_MODE == "groq":
        return _transcribe_groq(audio_path, total_duration)
    return _transcribe_local(audio_path, total_duration)