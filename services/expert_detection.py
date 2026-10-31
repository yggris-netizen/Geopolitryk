"""Определение эксперта(ов) по транскрипту, заголовку, описанию, LLM."""

import html
import json
import re

from config import GROQ_MODEL
from services.llm import groq_client
from experts import KNOWN_EXPERT_BIOS, KNOWN_EXPERT_FULL

try:
    import pymorphy3
    _MORPH = pymorphy3.MorphAnalyzer()
    MORPH_AVAILABLE = True
except ImportError:
    _MORPH = None
    MORPH_AVAILABLE = False
    print("⚠️  pymorphy3 не установлен")


_SURNAME_SUFFIXES = (
    "ов", "ев", "ёв", "ин", "ын", "ский", "цкий", "ская", "цкая",
    "ой", "ый", "ко", "ук", "юк", "ич", "енко", "чук", "дзе", "швили", "ян", "янц",
)

_CAPS_BLACKLIST = {
    "СРОЧНО", "ШОК", "ВАЖНО", "ГЛАВНОЕ", "ИТОГИ", "НОВОСТИ",
    "ПРЯМОЙ", "ЭФИР", "ПОЛНЫЙ", "ТУПИК", "ФРОНТ", "ФРОНТЕ",
    "КИЕВ", "ВОЙНА", "ВОЙНЫ", "МИР", "МИРА", "РОССИЯ", "УКРАИНА",
    "США", "ТРАМП", "ПУТИН", "ЗЕЛЕНСКИЙ", "ЗАЯВЛЕНИЕ", "ВИДЕО",
    "СМОТРЕТЬ", "ЭКСКЛЮЗИВ", "СЕНСАЦИЯ", "ПРЕЗИДЕНТ", "ПРЕЗИДЕНТА",
    "РЕШЕНИЕ", "СОРВАЛОСЬ", "ВСЁ", "ВСЕ", "НАЧАЛО",
    "КОНЕЦ", "ПРАВДА", "ЛОЖЬ", "ТАЙНА", "ТАЙНЫ", "ВЫБОРЫ",
    "РАЗВЯЗКА", "УЛЬТИМАТУМ", "ФИНАЛЬНЫЕ", "ПЕРЕГОВОРЫ",
    "ШОКИРУЮЩАЯ", "ПРЯМО", "СЕЙЧАС", "НОЧЬЮ", "НОЧЬ",
    "РАСХОДНИК", "ДВОЕЧНИК", "КАРТА", "ФРОНТА", "СДЕЛКА",
    "ГОТОВЯТ", "СЛУЧИЛОСЬ", "НОВОЕ", "НОВОСТЬ", "ОБЗОР",
    "СРОЧНЫЙ", "ГЛАВНЫЙ", "МОЛНИЯ", "ПОСЛЕДНИЙ", "ШОКИРУЮЩИЙ",
    "ДУПЛО", "БЕЛОЧКИ", "ЗАМОРОЗКА", "ОТМОРОЗКА", "РАЗБУШЕВ",
    "СДАВАТЬСЯ", "УСКОРИЛ", "ЯРОСТИ", "АГОНИИ",
    "РЕВА", "РЕВ", "РЁВ",
}


def _looks_like_surname(word: str) -> bool:
    if not word:
        return False
    return any(word.lower().endswith(s) for s in _SURNAME_SUFFIXES)


def _sanitize_expert_name(name: str) -> str:
    if not name or name == "Неизвестный эксперт":
        return name
    words = name.split()
    if not words:
        return "Неизвестный эксперт"
    for w in words:
        if w.upper() in _CAPS_BLACKLIST:
            print(f"      🧹 мусор в имени («{name}» → «Неизвестный эксперт»)")
            return "Неизвестный эксперт"
    if len(words) >= 2 and all(w.isupper() for w in words):
        print(f"      🧹 всё CAPS в имени: «{name}»")
        return "Неизвестный эксперт"

    _BAD_SUFFIXES = ("нуть", "тся", "ться", "ешь", "ишь", "уть", "ать", "ять", "ить")
    cleaned = []
    for w in words:
        for suf in _BAD_SUFFIXES:
            if w.lower().endswith(suf) and len(w) > len(suf) + 3:
                w_new = w[:-len(suf)]
                if _looks_like_surname(w_new) or (MORPH_AVAILABLE and _MORPH.parse(w_new)):
                    print(f"      🧹 убираю «{suf}»: {w} → {w_new}")
                    w = w_new
                    break
        cleaned.append(w)
    return " ".join(cleaned)


def _to_nominative(name: str) -> str:
    if not name or not MORPH_AVAILABLE:
        return name
    result = []
    for p in name.split():
        if len(p) < 3:
            result.append(p)
            continue
        parsed = _MORPH.parse(p)
        if not parsed:
            result.append(p)
            continue
        surn = next((h for h in parsed if 'Surn' in h.tag), None)
        if surn:
            nf = surn.normal_form
            result.append(nf.capitalize() if p[0].isupper() and nf.lower() != p.lower() else p)
            continue
        if p[0].isupper() and _looks_like_surname(p):
            result.append(p)
            continue
        nf = parsed[0].normal_form
        result.append(nf.capitalize() if p[0].isupper() and nf.lower() != p.lower() else p)
    return " ".join(result)


def _ensure_surname_first(name: str) -> str:
    if not name:
        return name
    words = name.split()
    if len(words) != 2:
        return name
    a, b = words
    if _looks_like_surname(b) and not _looks_like_surname(a):
        return f"{b} {a}"
    return name


def _finalize_name(name: str) -> str:
    if not name or name == "Неизвестный эксперт":
        return name
    name = _ensure_surname_first(_to_nominative(name))
    name = _sanitize_expert_name(name)
    key = name.lower().strip()
    if key in KNOWN_EXPERT_FULL and " " not in key:
        expanded = KNOWN_EXPERT_FULL[key]
        print(f"      📖 полное имя: {name} → {expanded}")
        name = expanded
    return name


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    name = name.strip().strip('"').strip("'").strip(".").strip()
    for prefix in ["г-н ", "господин ", "г-жа ", "госпожа ", "д-р ", "доктор "]:
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix):].strip()
    return re.sub(r'\s+', ' ', name)


def _looks_like_person_name(name: str) -> bool:
    if not name or len(name) < 5 or len(name) > 50:
        return False
    words = name.split()
    if not (2 <= len(words) <= 3):
        return False
    return all(w[:1].isupper() for w in words if w)


def _is_likely_person(name: str) -> bool:
    if not name or len(name) < 5 or len(name) > 50:
        return False
    words = name.split()
    if not (2 <= len(words) <= 3):
        return False
    if not all(w and w[0].isupper() for w in words):
        return False
    if MORPH_AVAILABLE:
        for w in words:
            parsed = _MORPH.parse(w)
            if parsed and any(k in str(parsed[0].tag) for k in ('Name', 'Surn', 'Patr')):
                return True
        for w in words:
            if any(w.lower().endswith(s) for s in _SURNAME_SUFFIXES):
                return True
            if w.lower().endswith(("ым", "им", "ом", "ем")):
                return True
        return False
    return True


def _extract_name_from_description(description: str) -> str:
    if not description:
        return ""
    patterns = [
        r'гость[ья]?\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'в\s+гостях\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'спикер\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'эксперт\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
    ]
    for pat in patterns:
        m = re.search(pat, description, re.IGNORECASE)
        if m:
            cand = _normalize_name(m.group(1))
            if _looks_like_person_name(cand):
                return cand
    return ""


def _extract_name_from_title(title: str) -> str:
    if not title:
        return ""
    title = html.unescape(title)

    m = re.search(r'\b(?:с|со)\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,2})\b', title)
    if m:
        raw = _normalize_name(m.group(1))
        nom = _to_nominative(raw)
        words = nom.split()
        if not any(w.upper() in _CAPS_BLACKLIST for w in words):
            if len(words) >= 2 and _is_likely_person(nom):
                return nom
            if len(words) == 1:
                if MORPH_AVAILABLE:
                    parsed = _MORPH.parse(words[0])
                    if parsed and 'Surn' in parsed[0].tag:
                        return words[0]
                if words[0].lower() in KNOWN_EXPERT_BIOS or words[0].lower() in KNOWN_EXPERT_FULL:
                    return words[0]

    m = re.search(r'([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\s+(?:для|у|в|на)\s+', title)
    if m:
        cand = _normalize_name(m.group(1))
        if _looks_like_person_name(cand):
            return cand

    m = re.search(r'Прогноз\s+([А-ЯЁ][а-яё]+)(?:\s|$|[,.!?])', title)
    if m:
        s = m.group(1)
        if s.endswith("и") or s.endswith("ы"):
            s = s[:-1] + "а"
        elif s.endswith("а"):
            s = s[:-1]
        return s

    for mm in re.finditer(r'\|\s*([А-ЯЁ]{3,}(?:\s+[А-ЯЁ][а-яё]+)?)\s*[-—:]\s', title):
        cc = mm.group(1).strip()
        if cc.split()[0] in _CAPS_BLACKLIST:
            continue
        cand = cc.title()
        if 3 <= len(cand) <= 40:
            print(f"      (CAPS: {cc} → {cand})")
            return cand

    for pat in [r'/\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)\s*$',
                r'\s-\s([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)\s*$']:
        m = re.search(pat, title)
        if m:
            cand = _normalize_name(m.group(1))
            if 1 <= len(cand.split()) <= 2 and cand and cand[0].isupper():
                return cand

    m = re.search(r'\|\s*([А-ЯЁ][а-яё]+)\s*-\s', title)
    if m:
        cand = _normalize_name(m.group(1))
        if 3 <= len(cand) <= 30:
            return cand

    clean = re.sub(r'[🍀🔥💥⚡️🎯📌]', '', title).strip()
    for sep in ["|", "—", " - ", ":"]:
        if sep in clean:
            cand = _normalize_name(clean.split(sep)[-1])
            if _looks_like_person_name(cand):
                return cand
    return ""


def _expand_surname_in_transcript(surname: str, transcript: str) -> str:
    if not surname or not transcript:
        return surname
    if len(surname.split()) > 1:
        return surname
    window = transcript[:5000]
    variants = [surname]
    if surname.title() != surname:
        variants.append(surname.title())
    if surname.upper() != surname:
        variants.append(surname.upper())
    for v in variants:
        for pat in [rf'([А-ЯЁ][а-яё]+)\s+{re.escape(v)}', rf'{re.escape(v)}\s+([А-ЯЁ][а-яё]+)']:
            m = re.search(pat, window)
            if m:
                part = m.group(1)
                if not _looks_like_surname(part) and 3 <= len(part) <= 25:
                    res = _to_nominative(f"{v.title()} {part}")
                    print(f"      (расширил: {surname} → {res})")
                    return res
    return surname.title() if surname.isupper() else surname


def _extract_name_from_intro(transcript: str) -> str:
    intro = transcript[:3000]
    patterns = [
        r'в гостях\s+(?:у нас\s+)?(?:был[аи]?|стал[аи]?)?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'(?:наш|мой)\s+гост[ья]\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'на\s+связи\s+(?:был[аи]?)?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'(?:в\s+эфире|в\s+студии)\s+(?:был[аи]?)?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+),?\s+(?:политолог|политик|экономист|журналист|эксперт|аналитик)',
        r'мой\s+собеседник\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
    ]
    for pat in patterns:
        m = re.search(pat, intro)
        if m:
            cand = _normalize_name(m.group(1))
            if _looks_like_person_name(cand):
                return cand
    return ""


def _is_owner_name(name: str, channel_owner: str) -> bool:
    if not name or not channel_owner:
        return False

    def _norm(s: str) -> str:
        return re.sub(r'[.\s]+', ' ', s.lower()).strip()

    n1 = _norm(name)
    n2 = _norm(channel_owner)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    w1 = n1.split()
    w2 = n2.split()
    if not w1 or not w2:
        return False
    if sorted(w1) == sorted(w2):
        return True
    if len(w1) == 2 and len(w2) == 2:
        for short, full in [(w1, w2), (w2, w1)]:
            a, b = short
            for ini, fam in [(a, b), (b, a)]:
                if len(ini) == 1 and len(fam) > 2:
                    for i, w in enumerate(full):
                        if w == fam:
                            other = full[1 - i] if 0 <= (1 - i) < len(full) else ""
                            if other and other[0] == ini:
                                return True
    for wa in w1:
        if len(wa) < 4:
            continue
        for wb in w2:
            if wa == wb and _looks_like_surname(wa):
                return True
    return False


# ─── Новая LLM-детекция: массив экспертов ───────────────────

def _parse_experts_json(raw: str) -> list[dict]:
    """Парсит JSON-массив [{"name": ..., "bio": ...}, ...]."""
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    if not raw:
        return []
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    result = []
    for item in arr[:5]:
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
            bio = (item.get("bio") or "").strip()[:60]
            if name and 2 <= len(name) <= 60:
                result.append({"name": name, "bio": bio})
        elif isinstance(item, str):
            s = item.strip()
            if s and 2 <= len(s) <= 60:
                result.append({"name": s, "bio": ""})
    return result


def _llm_detect_experts(video_title, transcript, channel_name, channel_type, channel_owner):
    """Возвращает список гостей (не автора канала)."""
    if channel_type == "author":
        hint = (f"АВТОРСКИЙ канал «{channel_name}». "
                f"Владелец — {channel_owner} — ВЕДУЩИЙ. Эксперты — ГОСТИ, НЕ ведущий.")
    elif channel_type == "personal":
        hint = (f"ПЕРСОНАЛЬНЫЙ канал «{channel_name}». Владелец — {channel_owner}.")
    else:
        hint = f"МЕДИЙНЫЙ канал «{channel_name}». Эксперты — ГОСТИ."

    prompt = f"""Определи ВСЕХ ЭКСПЕРТОВ-ГОСТЕЙ в этом видео.

КОНТЕКСТ:
- Канал: «{channel_name}», тип: {channel_type}
- {hint}
- Владелец канала: {channel_owner or '(нет)'}
- Заголовок: {video_title or "(без названия)"}

Верни JSON-массив объектов:
[{{"name": "Имя Фамилия", "bio": "политолог"}}, {{"name": "Другой Гость", "bio": "экономист"}}]

ПРАВИЛА:
1. Эксперт — гость, НЕ ведущий. Владельца канала («{channel_owner or ''}») НЕ включай.
2. Включай ВСЕХ гостей: 1, 2 или 3. Если два гостя — верни два объекта.
3. name — «Имя Фамилия» или «Фамилия Имя», в ИМЕНИТЕЛЬНОМ падеже.
4. bio — специализация из транскрипта (политолог, экономист, военный аналитик, депутат и т.п.). Коротко, до 40 символов. Если не определить — пустая строка.
5. Если в видео только ведущий без гостей — верни пустой массив [].
6. Если не определить — [].

Нет гостей → [].

Начало транскрипта:
---
{transcript[:8000]}
---

JSON:"""
    try:
        r = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=GROQ_MODEL, temperature=0.1, max_tokens=800,
        )
        raw = r.choices[0].message.content.strip()
        return _parse_experts_json(raw)
    except Exception as e:
        err = str(e).lower()
        if "429" in err or "rate_limit" in err:
            print("      ⏸  429 [experts] — пропускаю LLM")
            return []
        print(f"⚠️  LLM experts: {e}")
        return []


# ─── Главная функция: detect_experts ───────────────────────

def detect_experts(video_title, transcript, description="",
                   channel_name="", channel_type="media", channel_owner=""):
    """
    Возвращает список экспертов:
    [{"name": "Иванов Иван", "bio": "политолог", "role": "guest"}]
    [{"name": "Руслан Бортник", "bio": "", "role": "author"}]  — если выступает автор канала
    [{"name": "Неизвестный эксперт", "bio": "", "role": "unknown"}]
    """
    print("   Шаг 1: интро...")
    name = _extract_name_from_intro(transcript)
    if name:
        name = _finalize_name(name)
        if channel_type == "author" and _is_owner_name(name, channel_owner):
            print(f"      ⚠️  интро дало имя ведущего «{name}» — пропускаю")
        else:
            print(f"      ✅ {name}")
            bio = generate_expert_bio(name, transcript)
            return [{"name": name, "bio": bio, "role": "guest"}]
    print("      ⚠️  нет")

    print("   Шаг 2: заголовок...")
    name = _extract_name_from_title(video_title)
    if name:
        name = _expand_surname_in_transcript(name, transcript)
        name = _finalize_name(name)
        if channel_type == "author" and _is_owner_name(name, channel_owner):
            print(f"      ⚠️  заголовок дал имя ведущего «{name}» — пропускаю")
        else:
            print(f"      ✅ {name}")
            bio = generate_expert_bio(name, transcript)
            return [{"name": name, "bio": bio, "role": "guest"}]
    print("      ⚠️  нет")

    if description:
        print("   Шаг 3: описание...")
        name = _extract_name_from_description(description)
        if name:
            name = _finalize_name(name)
            if channel_type == "author" and _is_owner_name(name, channel_owner):
                print(f"      ⚠️  описание дало имя ведущего «{name}» — пропускаю")
            else:
                print(f"      ✅ {name}")
                bio = generate_expert_bio(name, transcript)
                return [{"name": name, "bio": bio, "role": "guest"}]
        print("      ⚠️  нет")

    # Эвристики не нашли гостя. Для author — экономия TPM: сразу fallback на канал.
    if channel_type == "author" and channel_owner:
        on = _finalize_name(channel_owner)
        print("   Шаг 4: LLM — пропускаю (author без гостя в эвристиках)")
        print(f"   Шаг 5: выступает автор канала: {on}")
        return [{"name": on, "bio": "", "role": "author"}]

    print("   Шаг 4: LLM (поиск гостей)...")
    guests = _llm_detect_experts(video_title, transcript, channel_name, channel_type, channel_owner)
    if guests:
        result = []
        for g in guests:
            n = _finalize_name(g.get("name", ""))
            if not n or n == "Неизвестный эксперт":
                continue
            if channel_type == "author" and _is_owner_name(n, channel_owner):
                continue  # это ведущий, не гость
            bio = generate_expert_bio(n, transcript) or g.get("bio", "")
            result.append({"name": n, "bio": bio, "role": "guest"})
        if result:
            print(f"      ✅ гостей: {len(result)}")
            return result
    print("      ⚠️  нет")

    if channel_type == "personal" and channel_owner:
        on = _finalize_name(channel_owner)
        print(f"   Шаг 5: fallback personal: {on}")
        return [{"name": on, "bio": "", "role": "author"}]

    print("   ⚠️  не определено")
    return [{"name": "Неизвестный эксперт", "bio": "", "role": "unknown"}]


# ─── Обёртка для обратной совместимости ─────────────────────

def detect_expert_name(video_title, transcript, description="",
                       channel_name="", channel_type="media", channel_owner=""):
    """Совместимость: возвращает строку с ФИО (объединённую через запятую)."""
    experts = detect_experts(video_title, transcript, description=description,
                             channel_name=channel_name, channel_type=channel_type,
                             channel_owner=channel_owner)
    if not experts:
        return "Неизвестный эксперт"
    if len(experts) == 1:
        return experts[0]["name"]
    return ", ".join(e["name"] for e in experts)


def generate_expert_bio(expert_name, transcript):
    if not expert_name or expert_name == "Неизвестный эксперт":
        return ""
    key = expert_name.lower().strip()
    if key in KNOWN_EXPERT_BIOS:
        return KNOWN_EXPERT_BIOS[key]
    fw = key.split()[0] if key.split() else ""
    if fw in KNOWN_EXPERT_BIOS:
        return KNOWN_EXPERT_BIOS[fw]
    return ""