"""Определение спикеров по голосу (voice fingerprinting) с фильтрацией шума
и дедупликацией известных по имени.

Порог MATCH_THRESHOLD = 0.78 (Артём 0.79 → match, Бизяев-дубликат 0.77 → near-match).
Дубликаты experts по имени — отбрасываются (не в unknown).
"""

import numpy as np

from db.voice_profiles import find_best_match
from services.speaker_embedding import cosine_similarity

MIN_UNKNOWN_DURATION_SEC = 30.0
UNKNOWN_DEDUP_THRESHOLD = 0.85
NEAR_MATCH_THRESHOLD = 0.70
MATCH_THRESHOLD = 0.78


def detect_experts(video_title, transcript, description="",
                   channel_name="", channel_type="media", channel_owner="",
                   speaker_transcript: str = "", clusters: list = None,
                   video_url: str = ""):
    """Возвращает (experts, unknown)."""
    if not clusters:
        print("   ⚠️  voice_identification: кластеров нет")
        return [], []

    print(f"   → Режим: voice fingerprinting ({len(clusters)} кластеров)")

    experts_raw = []
    unknown_candidates = []

    for cl in clusters:
        speaker_id = cl["speaker_id"]
        duration = cl.get("duration_sec", 0)

        match = find_best_match(cl["embedding"], threshold=MATCH_THRESHOLD)
        if match:
            experts_raw.append({
                "name": match["name"],
                "role": match.get("role", "guest"),
                "speaker": speaker_id,
                "duration_sec": duration,
                "score": match["score"],
            })
            print(f"   ✅ {speaker_id} → {match['name']} "
                  f"(score={match['score']:.2f}, {duration:.0f}с)")
            continue

        near = find_best_match(cl["embedding"], threshold=NEAR_MATCH_THRESHOLD)
        if near:
            print(f"   ⚠️  {speaker_id} → возможно, {near['name']} "
                  f"(score={near['score']:.2f}, {duration:.0f}с) — пропускаю")
            continue

        if duration < MIN_UNKNOWN_DURATION_SEC:
            print(f"   ⏭ {speaker_id} ({duration:.0f}с < {MIN_UNKNOWN_DURATION_SEC:.0f}с) "
                  f"— шум, пропускаю")
            continue

        unknown_candidates.append({
            "speaker_id": speaker_id,
            "embedding": cl["embedding"],
            "duration_sec": duration,
        })

    # Дедупликация experts по имени: оставляем с максимальным duration_sec,
    # дубликаты — отбрасываем (не в unknown)
    by_name = {}
    for e in experts_raw:
        name = e["name"]
        if name not in by_name:
            by_name[name] = e
        elif e["duration_sec"] > by_name[name]["duration_sec"]:
            print(f"   🔄 {by_name[name]['speaker']} — дубликат «{name}» "
                  f"({by_name[name]['duration_sec']:.0f}с < {e['duration_sec']:.0f}с), "
                  f"заменяю на {e['speaker']}")
            by_name[name] = e
        else:
            print(f"   🔄 {e['speaker']} — дубликат «{name}» "
                  f"({e['duration_sec']:.0f}с), отбрасываю")

    experts = []
    for name, e in by_name.items():
        experts.append({
            "name": e["name"],
            "role": e["role"],
            "speaker": e["speaker"],
            "duration_sec": e["duration_sec"],
        })
        print(f"   ✅ {e['speaker']} → {name} (kept, {e['duration_sec']:.0f}с)")

    # Дедупликация unknown между собой (cos > 0.85)
    unknown = []
    for cand in unknown_candidates:
        is_dup = False
        for kept in unknown:
            score = cosine_similarity(cand["embedding"], kept["embedding"])
            if score > UNKNOWN_DEDUP_THRESHOLD:
                if cand["duration_sec"] > kept["duration_sec"]:
                    print(f"   🔄 {kept['speaker_id']} — дубликат {cand['speaker_id']} "
                          f"(cos={score:.2f}), заменяю на длинный")
                    unknown.remove(kept)
                    unknown.append(cand)
                else:
                    print(f"   🔄 {cand['speaker_id']} — дубликат {kept['speaker_id']} "
                          f"(cos={score:.2f}), пропускаю")
                is_dup = True
                break
        if not is_dup:
            unknown.append(cand)

    if unknown:
        print(f"   ❓ Незнакомых (после фильтрации): {len(unknown)}")
        for u in unknown:
            print(f"      {u['speaker_id']}: {u['duration_sec']:.0f}с")

    return experts, unknown


def detect_expert_name(video_title, transcript, description="",
                       channel_name="", channel_type="media", channel_owner="",
                       speaker_transcript: str = "", clusters: list = None,
                       video_url: str = ""):
    experts, _ = detect_experts(
        video_title, transcript, description=description,
        channel_name=channel_name, channel_type=channel_type,
        channel_owner=channel_owner, speaker_transcript=speaker_transcript,
        clusters=clusters, video_url=video_url,
    )
    if not experts:
        return "Неизвестный спикер"
    return ", ".join(e["name"] for e in experts)