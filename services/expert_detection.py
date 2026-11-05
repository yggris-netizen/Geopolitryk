"""Определение спикеров по голосу (voice fingerprinting).

Логика:
1. Voice identification → кластеры спикеров.
2. Для каждого кластера — find_best_match в voice_profiles.
3. Совпало → experts (name, role из профиля).
4. Не совпало → unknown (на разметку владельцем).

Без NER, без pymorphy, без эвристик по заголовку — только голос.
"""

from db.voice_profiles import find_best_match


def detect_experts(video_title, transcript, description="",
                   channel_name="", channel_type="media", channel_owner="",
                   speaker_transcript: str = "", clusters: list = None,
                   video_url: str = ""):
    """
    Главная функция: voice-first.

    Возвращает (experts, unknown):
    - experts: [{name, role, speaker}]
    - unknown: [{speaker_id, embedding, duration_sec}] — на разметку
    """
    if not clusters:
        print("   ⚠️  voice_identification: кластеров нет")
        return [], []

    print(f"   → Режим: voice fingerprinting ({len(clusters)} кластеров)")

    experts = []
    unknown = []

    for cl in clusters:
        speaker_id = cl["speaker_id"]
        match = find_best_match(cl["embedding"], threshold=0.75)
        if match:
            experts.append({
                "name": match["name"],
                "role": match.get("role", "guest"),
                "speaker": speaker_id,
            })
            print(f"   ✅ {speaker_id} → {match['name']} "
                  f"(score={match['score']:.2f})")
        else:
            unknown.append({
                "speaker_id": speaker_id,
                "embedding": cl["embedding"],
                "duration_sec": cl["duration_sec"],
            })
            print(f"   ❓ {speaker_id} → незнакомый ({cl['duration_sec']:.0f}с)")

    if unknown:
        print(f"   ❓ Незнакомых: {len(unknown)}")

    return experts, unknown


def detect_expert_name(video_title, transcript, description="",
                       channel_name="", channel_type="media", channel_owner="",
                       speaker_transcript: str = "", clusters: list = None,
                       video_url: str = ""):
    """Обёртка для обратной совместимости — возвращает имена через запятую."""
    experts, _ = detect_experts(
        video_title, transcript, description=description,
        channel_name=channel_name, channel_type=channel_type,
        channel_owner=channel_owner, speaker_transcript=speaker_transcript,
        clusters=clusters, video_url=video_url,
    )
    if not experts:
        return "Неизвестный спикер"
    return ", ".join(e["name"] for e in experts)