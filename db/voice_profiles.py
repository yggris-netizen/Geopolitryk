"""CRUD для voice_profiles (Postgres, embedding BYTEA)."""
import numpy as np
import psycopg2.extras

from db.connection import get_conn


def _embedding_to_blob(embedding: np.ndarray) -> bytes:
    return embedding.astype(np.float32).tobytes()


def _blob_to_embedding(blob) -> np.ndarray:
    return np.frombuffer(bytes(blob), dtype=np.float32)


def save_voice_profile(name: str, bio: str, role: str, embedding: np.ndarray,
                       duration_sec: float = 0, source_video_id: str = "") -> int:
    if not name or embedding is None or len(embedding) == 0:
        return 0
    if role not in ("author", "guest"):
        return 0
    blob = _embedding_to_blob(embedding)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM voice_profiles WHERE name=%s AND role=%s",
                    (name, role))
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE voice_profiles SET bio=%s, embedding=%s, duration_sec=%s, "
                "source_video_id=%s, updated_at=NOW() WHERE id=%s",
                (bio, blob, duration_sec, source_video_id, existing[0])
            )
            conn.commit()
            return existing[0]
        cur.execute(
            "INSERT INTO voice_profiles (name, bio, role, embedding, duration_sec, source_video_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (name, bio, role, blob, duration_sec, source_video_id)
        )
        pid = cur.fetchone()[0]
        conn.commit()
        return pid
    except Exception as e:
        print(f"[WARN]  save_voice_profile: {e}")
        return 0
    finally:
        cur.close()
        conn.close()


def get_all_profiles() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id, name, bio, role, embedding, duration_sec, source_video_id, created_at "
            "FROM voice_profiles ORDER BY role, name"
        )
        result = []
        for r in cur.fetchall():
            d = dict(r)
            d["embedding"] = _blob_to_embedding(d["embedding"])
            result.append(d)
        return result
    finally:
        cur.close()
        conn.close()


def get_profile_by_name(name: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id, name, bio, role, embedding, duration_sec FROM voice_profiles "
            "WHERE name=%s ORDER BY role LIMIT 1", (name,)
        )
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["embedding"] = _blob_to_embedding(d["embedding"])
        return d
    finally:
        cur.close()
        conn.close()


def find_best_match(embedding: np.ndarray, threshold: float = 0.75) -> dict | None:
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
        return {"id": best["id"], "name": best["name"], "bio": best["bio"],
                "role": best["role"], "score": best_score}
    return None


def delete_profile(profile_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM voice_profiles WHERE id=%s", (profile_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def count_profiles() -> int:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM voice_profiles")
        return cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()
