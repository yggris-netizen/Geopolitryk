"""CLI для ручной регистрации эталонных голосов.

Использование:
    python scripts/register_voice.py --video "https://youtube.com/..." \\
        --start 120 --end 180 --name "Александр Шелест" --role author

    python scripts/register_voice.py --audio "path/to/audio.mp3" \\
        --start 60 --end 120 --name "Руслан Бизяев" --role guest --bio "политолог"

--start, --end — секунды от начала видео/аудио.
Если --video, аудио скачивается через yt-dlp (нужен cookies.txt).
"""

import argparse
import os
import sys

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import _init_db
from db.voice_profiles import count_profiles, save_voice_profile
from services.speaker_embedding import get_embedding


def main():
    parser = argparse.ArgumentParser(description="Регистрация эталонного голоса")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--video", help="URL YouTube (скачает аудио)")
    group.add_argument("--audio", help="Путь к локальному аудиофайлу")

    parser.add_argument("--start", type=float, default=0, help="Начало куска (сек)")
    parser.add_argument("--end", type=float, default=None, help="Конец куска (сек)")
    parser.add_argument("--name", required=True, help="ФИО эксперта/ведущего")
    parser.add_argument("--role", required=True, choices=["author", "guest"],
                        help="Роль: author (ведущий) или guest (эксперт)")
    parser.add_argument("--bio", default="", help="Специализация (для guest)")
    parser.add_argument("--duration", type=float, default=0,
                        help="Длительность куска (если не указан --end)")

    args = parser.parse_args()

    _init_db()
    print(f"📊 Профилей в БД до: {count_profiles()}")

    audio_path = args.audio
    if args.video:
        from services.transcription import download_audio
        print(f"📥 Скачиваю аудио из {args.video}...")
        audio_path = download_audio(args.video)
        print(f"✅ Скачано: {audio_path}")

    if not audio_path or not os.path.exists(audio_path):
        print(f"❌ Аудио не найдено: {audio_path}")
        sys.exit(1)

    start = args.start
    end = args.end
    if end is None:
        end = start + (args.duration or 60)

    print(f"🎤 Извлекаю embedding из {start:.0f}–{end:.0f}с...")
    emb = get_embedding(audio_path, start, end)
    if emb is None:
        print("❌ Не удалось извлечь embedding")
        sys.exit(1)
    print(f"✅ Embedding: {emb.shape}")

    pid = save_voice_profile(
        name=args.name, bio=args.bio, role=args.role,
        embedding=emb, duration_sec=(end - start),
        source_video_id=args.video or "",
    )
    if pid:
        print(f"✅ Сохранён профиль #{pid}: {args.name} ({args.role})")
    else:
        print("❌ Не удалось сохранить")
        sys.exit(1)

    # Удаляем скачанное аудио (если скачивали)
    if args.video and audio_path and os.path.exists(audio_path):
        try:
            os.remove(audio_path)
            print(f"🧹 Удалено: {audio_path}")
        except Exception:
            pass

    print(f"📊 Профилей в БД после: {count_profiles()}")


if __name__ == "__main__":
    main()