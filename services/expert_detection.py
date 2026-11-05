"""Определение спикеров по голосу (voice fingerprinting) с фильтрацией шума.

Логика:
1. Voice identification → кластеры спикеров.
2. Для каждого кластера — find_best_match (порог 0.75) → известные.
3. Неизвестные:
   - duration < MIN_UNKNOWN_DURATION_SEC (30с) → шум, пропускаем
   - near-match (0.70–0.75) → «возможно, X», пропускаем (не в pending)
   - дубликат другого unknown (cos > 0.85) → оставляем только самый длинный
   - иначе → в pending (реальный новый)
"""

import numpy as np

from db.voice_profiles import find_best_match, get_all_profiles
from services.speaker_embedding import cosine_similarity

MIN_UNKNOWN_DURATION_SEC = 30.0   # короче 30с — шум, не в pending
UNKNOWN_DEDUP_THRESHOLD = 0.85    # cos_sim > 0.85 → один и тот же голос
NEAR_MATCH_THRESHOLD = 0.70       # score 0.70–0.75 → «возможно, X»


def detect_experts(video_title, transcript, description="",
                   channel_name="", channel_type="media", channel_owner="",
                   speaker_transcript: str = "", clusters: list = None,
                   video_url: str = ""):
    """
    Главная функция: voice-first + фильтрация шума.

    Возвращает (experts, unknown):
    - experts: [{name, role, speaker}]
    - unknown: [{speaker_id, embedding, duration_sec}] — только реальные новые
    """
    if not clusters:
        print("   ⚠️  voice_identification: кластеров нет")
        return [], []

    print(f"   → Режим: voice fingerprinting ({len(clusters)} кластеров)")

    experts = []
    unknown_candidates = []  # [(cluster, near_match_info)]

    for cl in clusters:
        speaker_id = cl["speaker_id"]
        duration = cl.get("duration_sec", 0)

        # 1. Точное совпадение (>= 0.75)
        match = find_best_match(cl["embedding"], threshold=0.75)
        if match:
            experts.append({
                "name": match["name"],
                "role": match.get("role", "guest"),
                "speaker": speaker_id,
            })
            print(f"   ✅ {speaker_id} → {match['name']} "
                  f"(score={match['score']:.2f}, {duration:.0f}с)")
            continue

        # 2. Near-match (0.70–0.75) — «возможно, X», не в pending
        near = find_best_match(cl["embedding"], threshold=NEAR_MATCH_THRESHOLD)
        if near:
            print(f"   ⚠️  {speaker_id} → возможно, {near['name']} "
                  f"(score={near['score']:.2f}, {duration:.0f}с) — пропускаю")
            continue

        # 3. Слишком короткий — шум, не в pending
        if duration < MIN_UNKNOWN_DURATION_SEC:
            print(f"   ⏭ {speaker_id} ({duration:.0f}с < {MIN_UNKNOWN_DURATION_SEC:.0f}с) "
                  f"— шум, пропускаю")
            continue

        # 4. Кандидат в pending (дедупликация — ниже)
        unknown_candidates.append({
            "speaker_id": speaker_id,
            "embedding": cl["embedding"],
            "duration_sec": duration,
        })

    # 5. Дедупликация unknown между собой (cos > 0.85 — один голос)
    unknown = []
    for cand in unknown_candidates:
        is_dup = False
        for kept in unknown:
            score = cosine_similarity(cand["embedding"], kept["embedding"])
            if score > UNKNOWN_DEDUP_THRESHOLD:
                # Оставляем самый длинный
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

    # 6. Итог
    if unknown:
        print(f"   ❓ Незнакомых (после фильтрации): {len(unknown)}")
        for u in unknown:
            print(f"      {u['speaker_id']}: {u['duration_sec']:.0f}с")

    return experts, unknown


def detect_expert_name(video_title, transcript, description="",
                       channel_name="", channel_type="media", channel_owner="",
                       speaker_transcript: str = "", clusters: list = None,
                       video_url: str = ""):
    """Обёртка — возвращает имена через запятую."""
    experts, _ = detect_experts(
        video_title, transcript, description=description,
        channel_name=channel_name, channel_type=channel_type,
        channel_owner=channel_owner, speaker_transcript=speaker_transcript,
        clusters=clusters, video_url=video_url,
    )
    if not experts:
        return "Неизвестный спикер"
    return ", ".join(e["name"] for e in experts)