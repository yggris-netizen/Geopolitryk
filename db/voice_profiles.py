"""CRUD для voice_profiles: эталонные голоса экспертов и ведущих.

embedding хранится как BLOB (numpy float32 → bytes).
"""

import sqlite3

import numpy as np

from config import DB_PATH


def _embedding_to_blob(embedding: np.ndarray) -> bytes:
    """np.array (192,) → bytes (float32)."""
    return embedding.astype(np.float32).tobytes()


def _blob_to_embedding(blob: bytes) -> np.ndarray:
    """bytes → np.array (192,) float32."""
    return np.frombuffer(blob, dtype=np.float32)


def save_voice_profile(name: str, bio: str, role: str, embedding: np.ndarray,
                       duration_sec: float = 0, source_video_id: str = "") -> int:
    """Сохраняет или обновляет эталон голоса. Возвращает id."""
    if not name or embedding is None or len(embedding) == 0:
        return 0
    if role not in ("author", "guest"):
        return 0
    blob = _embedding_to_blob(embedding)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        # Если уже есть такой же name + role — обновляем
        existing = conn.execute(
            "SELECT id FROM voice_profiles WHERE name = ? AND role = ?",
            (name, role)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE voice_profiles SET bio = ?, embedding = ?, duration_sec = ?, "
                "source_video_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (bio, blob, duration_sec, source_video_id, existing[0])
            )
            conn.commit()
            return existing[0]
        cur = conn.execute(
            "INSERT INTO voice_profiles (name, bio, role, embedding, duration_sec, source_video_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, bio, role, blob, duration_sec, source_video_id)
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        print(f"[WARN]  save_voice_profile: {e}")
        return 0
    finally:
        conn.close()


def get_all_profiles() -> list[dict]:
    """Возвращает все эталоны: [{id, name, bio, role, embedding, duration_sec}]."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, bio, role, embedding, duration_sec, source_video_id, created_at "
        "FROM voice_profiles ORDER BY role, name"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["embedding"] = _blob_to_embedding(d["embedding"])
        result.append(d)
    return result


def get_profile_by_name(name: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, name, bio, role, embedding, duration_sec FROM voice_profiles "
        "WHERE name = ? ORDER BY role LIMIT 1",
        (name,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["embedding"] = _blob_to_embedding(d["embedding"])
    return d


def find_best_match(embedding: np.ndarray, threshold: float = 0.75) -> dict | None:
    """Ищет ближайший эталон по косинусному сходству.

    Возвращает {id, name, bio, role, score} или None, если score < threshold.
    """
    if embedding is None or len(embedding) == 0:
        return None
    profiles = get_all_profiles()
    if not profiles:
        return None

    emb_norm = embedding / (np.linalg.norm(embedding) + 1e-8)
    best = None
    best_score = -1.0

    for p in profiles:
        p_emb = p["embedding"]
        p_norm = p_emb / (np.linalg.norm(p_emb) + 1e-8)
        score = float(np.dot(emb_norm, p_norm))
        if score > best_score:
            best_score = score
            best = p

    if best and best_score >= threshold:
        return {
            "id": best["id"],
            "name": best["name"],
            "bio": best["bio"],
            "role": best["role"],
            "score": best_score,
        }
    return None


def delete_profile(profile_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        cur = conn.execute("DELETE FROM voice_profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_profiles() -> int:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    n = conn.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0]
    conn.close()
    return n