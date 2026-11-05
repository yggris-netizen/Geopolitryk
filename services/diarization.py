"""Диаризация аудио через pyannote.audio (community-1).

Обходит torchcodec: аудио загружается через soundfile в память,
pyannote получает тензор, а не путь к файлу.

Fallback: если soundfile не читает mp3 — конвертация mp3 → wav через ffmpeg,
затем чтение wav через soundfile.
"""

import os
import subprocess
import tempfile

import numpy as np
import torch

from config import (
    DIARIZATION_ENABLED, DIARIZATION_MAX_SPEAKERS, DIARIZATION_MIN_SPEAKERS,
    HF_TOKEN,
)

_pipeline = None
PYANNOTE_AVAILABLE = False

if DIARIZATION_ENABLED:
    if not HF_TOKEN:
        print("⚠️  DIARIZATION_ENABLED=1, но HF_TOKEN не задан — диаризация отключена")
    else:
        try:
            from pyannote.audio import Pipeline
            print("📥 Загружаю pyannote pipeline community-1 (может занять 30–60 секунд)...")
            _pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-community-1",
                token=HF_TOKEN,
            )
            PYANNOTE_AVAILABLE = True
            print("✅ Диаризация OK (pyannote community-1, soundfile-режим)")
        except Exception as e:
            print(f"❌ Диаризация не загрузилась: {e}")
            PYANNOTE_AVAILABLE = False
else:
    print("ℹ️  Диаризация отключена (DIARIZATION_ENABLED=0)")


def _load_audio_to_memory(audio_path: str):
    """
    Загружает аудио в память через soundfile (без torchcodec).
    Возвращает (torch.Tensor waveform [1, N], int sample_rate).
    Fallback: mp3 → wav через ffmpeg → soundfile.
    """
    try:
        import soundfile as sf
        data, sample_rate = sf.read(audio_path, dtype="float32", always_2d=True)
        # data: [N, channels] → [channels, N]
        waveform = torch.from_numpy(data.T)
        # Если стерео — усредняем в моно
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        return waveform, int(sample_rate)
    except Exception as e_sf:
        # Fallback: mp3 → wav через ffmpeg
        print(f"   ⚠️  soundfile не прочитал {os.path.basename(audio_path)}: {e_sf}")
        print(f"   🔄 Конвертирую в wav через ffmpeg...")
        try:
            import soundfile as sf
            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
                    check=True, capture_output=True, timeout=300,
                )
                data, sample_rate = sf.read(wav_path, dtype="float32", always_2d=True)
                waveform = torch.from_numpy(data.T)
                if waveform.shape[0] > 1:
                    waveform = waveform.mean(dim=0, keepdim=True)
                return waveform, int(sample_rate)
            finally:
                if os.path.exists(wav_path):
                    try:
                        os.remove(wav_path)
                    except Exception:
                        pass
        except Exception as e_ff:
            print(f"   ❌ ffmpeg fallback: {e_ff}")
            raise


def diarize(audio_path: str) -> list:
    """Возвращает [{start, end, speaker}] или [] если диаризация недоступна."""
    if not PYANNOTE_AVAILABLE or not _pipeline:
        return []
    if not os.path.exists(audio_path):
        print(f"⚠️  diarize: файл не найден {audio_path}")
        return []
    try:
        print(f"🎭 Диаризация: {os.path.basename(audio_path)}...")
        waveform, sample_rate = _load_audio_to_memory(audio_path)
        audio_in_memory = {"waveform": waveform, "sample_rate": sample_rate}
        print(f"   🎵 Аудио загружено: {waveform.shape[1]} сэмплов, {sample_rate} Гц")

        output = _pipeline(
            audio_in_memory,
            min_speakers=DIARIZATION_MIN_SPEAKERS,
            max_speakers=DIARIZATION_MAX_SPEAKERS,
        )
        annotation = getattr(output, "speaker_diarization", output)
        result = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            result.append({
                "start": float(turn.start),
                "end": float(turn.end),
                "speaker": str(speaker),
            })
        result.sort(key=lambda x: x["start"])
        speakers = {}
        for r in result:
            speakers[r["speaker"]] = speakers.get(r["speaker"], 0) + (r["end"] - r["start"])
        print(f"   ✅ {len(result)} реплик, спикеров: {len(speakers)}")
        for spk, dur in sorted(speakers.items(), key=lambda x: -x[1]):
            print(f"      {spk}: {dur:.1f}с")
        return result
    except Exception as e:
        print(f"⚠️  diarize: {e}")
        return []


def assign_speakers(whisper_segments: list, diarization: list) -> list:
    """Накладывает спикеров на сегменты Whisper по пересечению таймкодов."""
    if not whisper_segments:
        return []
    if not diarization:
        return [{**s, "speaker": "UNKNOWN"} for s in whisper_segments]

    result = []
    for seg in whisper_segments:
        seg_start = float(seg.get("start", 0))
        seg_end = float(seg.get("end", 0))
        best_speaker = "UNKNOWN"
        best_overlap = 0.0
        for turn in diarization:
            overlap = max(0.0, min(seg_end, turn["end"]) - max(seg_start, turn["start"]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = turn["speaker"]
        result.append({
            "start": seg_start,
            "end": seg_end,
            "text": seg.get("text", ""),
            "speaker": best_speaker,
        })
    return result


def build_speaker_transcript(segments_with_speakers: list, max_chars: int = 8000) -> str:
    """Собирает транскрипт с метками спикеров для LLM."""
    if not segments_with_speakers:
        return ""
    grouped = []
    current_speaker = None
    current_text = []
    for seg in segments_with_speakers:
        spk = seg.get("speaker", "UNKNOWN")
        text = seg.get("text", "").strip()
        if not text:
            continue
        if spk != current_speaker:
            if current_text:
                grouped.append((current_speaker, " ".join(current_text)))
            current_speaker = spk
            current_text = [text]
        else:
            current_text.append(text)
    if current_text:
        grouped.append((current_speaker, " ".join(current_text)))

    lines = []
    total = 0
    for spk, text in grouped:
        line = f"[{spk}]: {text}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)