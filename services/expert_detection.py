"""Определение спикеров по голосу (voice fingerprinting).

Порядок:
1. duration < MIN_MATCH_DURATION_SEC (20с) → шум (даже если match).
2. find_top_n_matches (>= 0.78) → топ-2 профиля.
3. NEGATIVE CHECK: если два профиля очень похожи (cos > 0.90)
   и разница в score < 0.03 → неоднозначно → в unknown (pending).
4. match = top[0] → experts.
5. near-match (0.70–0.78) → «возможно, X», пропуск.
6. duration < MIN_UNKNOWN_DURATION_SEC (30с) → шум (не в unknown).
7. иначе → unknown.

Оптимизация: один проход по профилям (_find_top_n_matches) вместо
find_best_match + _find_top_2_matches.
"""

import numpy as np

from db.voice_profiles import get_all_profiles
from services.speaker_embedding import cosine_similarity
from experts import KNOWN_EXPERT_BIOS, KNOWN_EXPERT_FULL
from config import (
    NEGATIVE_COS_THRESHOLD as NEGATIVE_COS_THRESHOLD_CONFIG,
    NEGATIVE_DIFF_THRESHOLD as NEGATIVE_DIFF_THRESHOLD_CONFIG,
)

MIN_MATCH_DURATION_SEC = 20.0
MIN_UNKNOWN_DURATION_SEC = 30.0
UNKNOWN_DEDUP_THRESHOLD = 0.85
NEAR_MATCH_THRESHOLD = 0.70
MATCH_THRESHOLD = 0.78

# Negative check: если два профиля cos > 0.90 и diff score < 0.03 → неоднозначно
NEGATIVE_COS_THRESHOLD = NEGATIVE_COS_THRESHOLD_CONFIG
NEGATIVE_DIFF_THRESHOLD = NEGATIVE_DIFF_THRESHOLD_CONFIG


def _find_top_n_matches(embedding, profiles: list, threshold: float, n: int = 2) -> list:
    """Возвращает топ-n профилей с score >= threshold, по убыванию score."""
    scores = []
    for p in profiles:
        try:
            sim = cosine_similarity(embedding, p["embedding"])
        except Exception:
            continue
        if sim >= threshold:
            scores.append((p, float(sim)))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:n]




def _get_specialization(name: str) -> str:
    """Возвращает специализацию эксперта из KNOWN_EXPERT_BIOS."""
    if not name:
        return ""
    key = name.lower().strip()
    # Прямое совпадение
    if key in KNOWN_EXPERT_BIOS:
        return KNOWN_EXPERT_BIOS[key]
    # По частям (имя фамилия / фамилия имя)
    parts = key.split()
    if len(parts) >= 2:
        # "имя фамилия" -> "фамилия имя"
        reversed_key = f"{parts[-1]} {parts[0]}"
        if reversed_key in KNOWN_EXPERT_BIOS:
            return KNOWN_EXPERT_BIOS[reversed_key]
    # Односложное (Пасков -> Пасков Пламен)
    if key in KNOWN_EXPERT_FULL:
        full = KNOWN_EXPERT_FULL[key]
        full_key = full.lower().strip()
        if full_key in KNOWN_EXPERT_BIOS:
            return KNOWN_EXPERT_BIOS[full_key]
    return ""

def _match_experts_in_text(text: str) -> list:
    """Ищет полные имена (2+ слова) из KNOWN_EXPERT_BIOS в тексте (без односложных)."""
    if not text:
        return []
    low = text.lower()
    found = []
    seen = set()
    for key, spec in KNOWN_EXPERT_BIOS.items():
        parts = key.split()
        if len(parts) < 2:
            continue  # односложные (сытник, дубов) — пропускаем
        if key in low and key not in seen:
            seen.add(key)
            found.append({"key": key, "spec": spec, "role": "guest"})
    return found


def _fallback_experts(description: str, video_title: str, channel_name: str) -> list:
    """Fallback: description -> channel_raw.

    ВАЖНО:
      - video_title НЕ ищем (тупик: заголовки обрезаны, "Ще..." вместо "Щелин").
      - channel_name НЕ нормализуем (не сравниваем с KNOWN_EXPERT_BIOS по словам).
      - channel_name идёт как author "как есть" (последний резерв).
    """
    # 1. description — поиск полных имён (2+ слова)
    matches = _match_experts_in_text(description or "")
    if matches:
        experts = []
        for m in matches:
            name = m["key"]
            experts.append({
                "name": name.title() if name == name.lower() else name,
                "role": "guest",
                "specialization": m["spec"],
                "speaker": "",
                "duration_sec": 0,
                "score": 0.0,
                "_fallback": "description",
            })
        return experts

    # 2. последний резерв — channel_name как author, БЕЗ нормализации
    if (channel_name or "").strip():
        return [{
            "name": channel_name.strip(),
            "role": "author",
            "specialization": "",
            "speaker": "",
            "duration_sec": 0,
            "score": 0.0,
            "_fallback": "channel_raw",
        }]
    return []


def detect_experts(video_title, transcript, description="",
                   channel_name="", channel_type="media", channel_owner="",
                   speaker_transcript: str = "", clusters: list = None,
                   video_url: str = ""):
    """Возвращает (experts, unknown)."""
    if not clusters:
        print("   [WARN]  voice_identification: кластеров нет -> fallback")
        fb = _fallback_experts(description or "", video_title or "", channel_name or "")
        if fb:
            src = fb[0].get("_fallback", "?")
            print(f"   [OK] fallback ({src}): {', '.join(e['name'] for e in fb)}")
            return fb, []
        return [], []

    print(f"   → Режим: voice fingerprinting ({len(clusters)} кластеров)")

    profiles = get_all_profiles()  # один раз на вызов
    experts_raw = []
    unknown_candidates = []

    for cl in clusters:
        speaker_id = cl["speaker_id"]
        duration = cl.get("duration_sec", 0)

        # 1. Слишком короткий для match — шум
        if duration < MIN_MATCH_DURATION_SEC:
            print(f"   [SKIP] {speaker_id} ({duration:.0f}с < {MIN_MATCH_DURATION_SEC:.0f}с) "
                  f"— шум, пропускаю")
            continue

        # 2. Топ-2 совпадения (>= MATCH_THRESHOLD)
        top2 = _find_top_n_matches(cl["embedding"], profiles,
                                    threshold=MATCH_THRESHOLD, n=2)

        if top2:
            # ─── NEGATIVE CHECK ───
            if len(top2) >= 2:
                first_p, first_s = top2[0]
                second_p, second_s = top2[1]
                try:
                    cos_between = cosine_similarity(
                        first_p["embedding"], second_p["embedding"]
                    )
                except Exception:
                    cos_between = 0.0
                diff = first_s - second_s
                if (cos_between > NEGATIVE_COS_THRESHOLD
                        and diff < NEGATIVE_DIFF_THRESHOLD):
                    print(f"   [WARN]  {speaker_id} — НЕОДНОЗНАЧНО: "
                          f"{first_p['name']} ({first_s:.2f}) vs "
                          f"{second_p['name']} ({second_s:.2f}), "
                          f"cos={cos_between:.2f}, diff={diff:.3f} → pending")
                    unknown_candidates.append({
                        "speaker_id": speaker_id,
                        "embedding": cl["embedding"],
                        "duration_sec": duration,
                    })
                    continue

            # Обычный match (top[0])
            first_p, first_s = top2[0]
            experts_raw.append({
                "name": first_p["name"],
                "role": first_p.get("role", "guest"),
                "specialization": _get_specialization(first_p["name"]),
                "speaker": speaker_id,
                "duration_sec": duration,
                "score": first_s,
            })
            print(f"   [OK] {speaker_id} → {first_p['name']} "
                  f"(score={first_s:.2f}, {duration:.0f}с)")
            continue

        # 3. Near-match (0.70–0.78) — «возможно, X», не в pending
        near = _find_top_n_matches(cl["embedding"], profiles,
                                    threshold=NEAR_MATCH_THRESHOLD, n=1)
        if near:
            np_, ns_ = near[0]
            print(f"   [WARN]  {speaker_id} → возможно, {np_['name']} "
                  f"(score={ns_:.2f}, {duration:.0f}с) — пропускаю")
            continue

        # 4. Слишком короткий для unknown — шум
        if duration < MIN_UNKNOWN_DURATION_SEC:
            print(f"   [SKIP] {speaker_id} ({duration:.0f}с < {MIN_UNKNOWN_DURATION_SEC:.0f}с) "
                  f"— шум, пропускаю")
            continue

        # 5. Кандидат в unknown
        unknown_candidates.append({
            "speaker_id": speaker_id,
            "embedding": cl["embedding"],
            "duration_sec": duration,
        })

    # Дедупликация experts по имени (оставляем с большим duration_sec)
    by_name = {}
    for e in experts_raw:
        name = e["name"]
        if name not in by_name:
            by_name[name] = e
        elif e["duration_sec"] > by_name[name]["duration_sec"]:
            print(f"   [RETRY] {by_name[name]['speaker']} — дубликат «{name}» "
                  f"({by_name[name]['duration_sec']:.0f}с < {e['duration_sec']:.0f}с), "
                  f"заменяю на {e['speaker']}")
            by_name[name] = e
        else:
            print(f"   [RETRY] {e['speaker']} — дубликат «{name}» "
                  f"({e['duration_sec']:.0f}с), отбрасываю")

    experts = []
    for name, e in by_name.items():
        experts.append({
            "name": e["name"],
            "role": e["role"],
            "specialization": e.get("specialization", ""),
            "speaker": e["speaker"],
            "duration_sec": e["duration_sec"],
        })
        print(f"   [OK] {e['speaker']} → {name} (kept, {e['duration_sec']:.0f}с)")

    # v3: если voice не определил никого (clusters были, но профили не совпали) -> fallback
    if not experts:
        print("   [WARN]  voice: профили не совпали -> fallback")
        fb = _fallback_experts(description or "", video_title or "", channel_name or "")
        if fb:
            src = fb[0].get("_fallback", "?")
            names = ", ".join(x["name"] for x in fb)
            print(f"   [OK] fallback ({src}): {names}")
            experts = fb

    # Дедупликация unknown между собой (cos > 0.85)
    unknown = []
    for cand in unknown_candidates:
        is_dup = False
        for kept in unknown:
            score = cosine_similarity(cand["embedding"], kept["embedding"])
            if score > UNKNOWN_DEDUP_THRESHOLD:
                if cand["duration_sec"] > kept["duration_sec"]:
                    print(f"   [RETRY] {kept['speaker_id']} — дубликат {cand['speaker_id']} "
                          f"(cos={score:.2f}), заменяю на длинный")
                    unknown.remove(kept)
                    unknown.append(cand)
                else:
                    print(f"   [RETRY] {cand['speaker_id']} — дубликат {kept['speaker_id']} "
                          f"(cos={score:.2f}), пропускаю")
                is_dup = True
                break
        if not is_dup:
            unknown.append(cand)

    if unknown:
        print(f"   [?] Незнакомых (после фильтрации): {len(unknown)}")
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