"""CRUD для pending_voices: незнакомые голоса на разметку владельцем."""

import sqlite3

import numpy as np

from config import DB_PATH


def _embedding_to_blob(embedding: np.ndarray) -> bytes:
    return embedding.astype(np.float32).tobytes()


def _blob_to_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def save_pending(video_id: str, video_url: str, embedding: np.ndarray,
                 duration_sec: float = 0, role_guess: str = "",
                 channel_name: str = "", channel_type: str = "",
                 speaker_id: str = "") -> int:
    """Сохраняет незнакомый голос в pending_voices. Возвращает id (0 если ошибка)."""
    if not video_id or embedding is None or len(embedding) == 0:
        return 0
    blob = _embedding_to_blob(embedding)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        existing = conn.execute(
            "SELECT id, embedding FROM pending_voices WHERE status = 'pending'"
        ).fetchall()
        emb_norm = embedding / (np.linalg.norm(embedding) + 1e-8)
        for row in existing:
            ex_emb = _blob_to_embedding(row[1])
            ex_norm = ex_emb / (np.linalg.norm(ex_emb) + 1e-8)
            score = float(np.dot(emb_norm, ex_norm))
            if score > 0.85:
                return row[0]
        cur = conn.execute(
            "INSERT INTO pending_voices "
            "(video_id, video_url, speaker_id, channel_name, channel_type, "
            " embedding, duration_sec, role_guess, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (video_id, video_url, speaker_id or "",
             channel_name or "", channel_type or "",
             blob, duration_sec, role_guess or "")
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        print(f"[WARN]  save_pending: {e}")
        return 0
    finally:
        conn.close()


def get_pending(limit: int = 10) -> list[dict]:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, video_id, video_url, speaker_id, channel_name, channel_type, "
        "       embedding, duration_sec, role_guess, created_at "
        "FROM pending_voices WHERE status = 'pending' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["embedding"] = _blob_to_embedding(d["embedding"])
        result.append(d)
    return result


def get_pending_by_id(pending_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM pending_voices WHERE id = ?", (pending_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["embedding"] = _blob_to_embedding(d["embedding"])
    return d


def mark_resolved(pending_id: int, status: str = "resolved") -> bool:
    if status not in ("resolved", "ignored"):
        status = "resolved"
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        cur = conn.execute(
            "UPDATE pending_voices SET status = ?, resolved_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (status, pending_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_pending() -> int:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    n = conn.execute("SELECT COUNT(*) FROM pending_voices WHERE status = 'pending'").fetchone()[0]
    conn.close()
    return n
