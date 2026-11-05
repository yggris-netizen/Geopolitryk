"""Идентификация спикеров по голосу.

1. Нарезка аудио на куски 10–30 сек (по сегментам Whisper).
2. ВЫБОРКА: берём не все куски, а равномерно 10–15 (для скорости).
3. Для каждого выбранного куска — embedding (SpeechBrain ECAPA).
4. Кластеризация по косинусному сходству (порог 0.8).
5. Для каждого кластера — mean embedding, суммарная длительность.
6. Возврат: [{speaker_id, embedding, duration_sec, chunks}, ...]
"""

import numpy as np

from services.speaker_embedding import cosine_similarity, get_embedding

MIN_CHUNK_SEC = 10.0
MAX_CHUNK_SEC = 30.0
MIN_SPEAKER_DURATION_SEC = 15.0
CLUSTER_THRESHOLD = 0.8
MAX_CHUNKS_TO_PROCESS = 12


def _group_segments(segments: list, min_sec: float = MIN_CHUNK_SEC,
                    max_sec: float = MAX_CHUNK_SEC) -> list:
    """Группирует соседние Whisper-сегменты в куски [start, end] длиной 10–30 сек."""
    if not segments:
        return []
    chunks = []
    cur_start = None
    cur_end = None
    for seg in segments:
        s = float(seg.get("start", 0))
        e = float(seg.get("end", 0))
        if e <= s:
            continue
        if cur_start is None:
            cur_start = s
            cur_end = e
        elif (e - cur_start) <= max_sec:
            cur_end = e
        else:
            if (cur_end - cur_start) >= min_sec:
                chunks.append([cur_start, cur_end])
            cur_start = s
            cur_end = e
    if cur_start is not None and (cur_end - cur_start) >= min_sec:
        chunks.append([cur_start, cur_end])
    return chunks


def _sample_chunks(chunks: list, max_n: int = MAX_CHUNKS_TO_PROCESS) -> list:
    """Равномерная выборка max_n кусков из всех (для скорости)."""
    if len(chunks) <= max_n:
        return chunks
    step = len(chunks) / max_n
    indices = [int(i * step) for i in range(max_n)]
    seen = set()
    result = []
    for idx in indices:
        if idx not in seen and idx < len(chunks):
            seen.add(idx)
            result.append(chunks[idx])
    return result


def identify_speakers(audio_path: str, transcript_segments: list,
                      max_chunks: int = MAX_CHUNKS_TO_PROCESS) -> list:
    """Идентифицирует спикеров в аудио."""
    if not audio_path or not transcript_segments:
        return []

    all_chunks = _group_segments(transcript_segments)
    if not all_chunks:
        print("⚠️  voice_identification: нет кусков >= 10 сек")
        return []

    chunks = _sample_chunks(all_chunks, max_chunks)
    print(f"🎤 Voice identification: {len(chunks)} кусков (из {len(all_chunks)} всего)...")

    chunk_embeddings = []
    for i, (start, end) in enumerate(chunks):
        emb = get_embedding(audio_path, start, end)
        if emb is None:
            continue
        chunk_embeddings.append({
            "start": start,
            "end": end,
            "embedding": emb,
            "duration": end - start,
        })
        print(f"   [{i+1}/{len(chunks)}] {start:.0f}–{end:.0f}с → embedding OK")

    if not chunk_embeddings:
        print("⚠️  voice_identification: не удалось извлечь ни одного embedding")
        return []

    clusters = []
    for ce in chunk_embeddings:
        placed = False
        for cl in clusters:
            mean_emb = np.mean(cl["embeddings"], axis=0)
            score = cosine_similarity(ce["embedding"], mean_emb)
            if score >= CLUSTER_THRESHOLD:
                cl["embeddings"].append(ce["embedding"])
                cl["chunks"].append({
                    "start": ce["start"],
                    "end": ce["end"],
                    "duration": ce["duration"],
                })
                placed = True
                break
        if not placed:
            clusters.append({
                "embeddings": [ce["embedding"]],
                "chunks": [{
                    "start": ce["start"],
                    "end": ce["end"],
                    "duration": ce["duration"],
                }],
            })

    result = []
    for idx, cl in enumerate(clusters):
        total_dur = sum(c["duration"] for c in cl["chunks"])
        if total_dur < MIN_SPEAKER_DURATION_SEC:
            print(f"   ⏭  кластер {idx}: {total_dur:.1f}с < {MIN_SPEAKER_DURATION_SEC}с — пропускаю")
            continue
        mean_emb = np.mean(cl["embeddings"], axis=0).astype(np.float32)
        result.append({
            "speaker_id": f"SPK_{idx:02d}",
            "embedding": mean_emb,
            "duration_sec": total_dur,
            "chunks": cl["chunks"],
        })

    result.sort(key=lambda x: -x["duration_sec"])
    print(f"   ✅ Найдено спикеров: {len(result)}")
    for r in result:
        print(f"      {r['speaker_id']}: {r['duration_sec']:.1f}с")
    return result


def assign_speakers_to_segments(transcript_segments: list, clusters: list) -> list:
    """Присваивает каждому сегменту speaker_id по пересечению с чанками кластеров."""
    if not transcript_segments:
        return []
    if not clusters:
        return [{**s, "speaker": "UNKNOWN"} for s in transcript_segments]

    result = []
    for seg in transcript_segments:
        seg_start = float(seg.get("start", 0))
        seg_end = float(seg.get("end", 0))
        best_speaker = "UNKNOWN"
        best_overlap = 0.0
        for cluster in clusters:
            for chunk in cluster["chunks"]:
                chunk_start = float(chunk["start"])
                chunk_end = float(chunk["end"])
                overlap = max(0.0, min(seg_end, chunk_end) - max(seg_start, chunk_start))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = cluster["speaker_id"]
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
