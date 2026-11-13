"""CRUD для pending_voices (Postgres, embedding BYTEA)."""
import numpy as np
import psycopg2.extras

from db.connection import get_conn


def _embedding_to_blob(embedding: np.ndarray) -> bytes:
    return embedding.astype(np.float32).tobytes()


def _blob_to_embedding(blob) -> np.ndarray:
    return np.frombuffer(bytes(blob), dtype=np.float32)


def save_pending(video_id: str, video_url: str, embedding: np.ndarray,
                 duration_sec: float = 0, role_guess: str = "",
                 channel_name: str = "", channel_type: str = "",
                 speaker_id: str = "") -> int:
    if not video_id or embedding is None or len(embedding) == 0:
        return 0
    blob = _embedding_to_blob(embedding)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, embedding FROM pending_voices WHERE status='pending'"
        )
        existing = cur.fetchall()
        emb_norm = embedding / (np.linalg.norm(embedding) + 1e-8)
        for row in existing:
            ex_emb = _blob_to_embedding(row[1])
            ex_norm = ex_emb / (np.linalg.norm(ex_emb) + 1e-8)
            score = float(np.dot(emb_norm, ex_norm))
            if score > 0.85:
                return row[0]
        cur.execute(
            "INSERT INTO pending_voices "
            "(video_id, video_url, speaker_id, channel_name, channel_type, "
            " embedding, duration_sec, role_guess, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending') RETURNING id",
            (video_id, video_url, speaker_id or "", channel_name or "",
             channel_type or "", blob, duration_sec, role_guess or "")
        )
        pid = cur.fetchone()[0]
        conn.commit()
        return pid
    except Exception as e:
        print(f"[WARN]  save_pending: {e}")
        return 0
    finally:
        cur.close()
        conn.close()


def get_pending(limit: int = 10) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id, video_id, video_url, speaker_id, channel_name, channel_type, "
            "       embedding, duration_sec, role_guess, created_at "
            "FROM pending_voices WHERE status='pending' "
            "ORDER BY created_at DESC LIMIT %s", (limit,)
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


def get_pending_by_id(pending_id: int) -> dict | None:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM pending_voices WHERE id=%s", (pending_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["embedding"] = _blob_to_embedding(d["embedding"])
        return d
    finally:
        cur.close()
        conn.close()


def mark_resolved(pending_id: int, status: str = "resolved") -> bool:
    if status not in ("resolved", "ignored"):
        status = "resolved"
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE pending_voices SET status=%s, resolved_at=NOW() WHERE id=%s",
            (status, pending_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def count_pending() -> int:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM pending_voices WHERE status='pending'")
        return cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()
