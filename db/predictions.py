"""CRUD для predictions."""

import sqlite3

from config import DB_PATH


def _save_predictions(video_id, expert_name, channel_name, predictions_list):
    if not predictions_list:
        return 0
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    saved = 0
    try:
        for p in predictions_list:
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO predictions "
                    "(video_id, expert_name, channel_name, prediction_text) VALUES (?, ?, ?, ?)",
                    (video_id, expert_name, channel_name, p)
                )
                if cur.rowcount > 0:
                    saved += 1
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    return saved


def _recent_predictions(limit=30):
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, expert_name, channel_name, prediction_text, "
        "       prediction_date, verdict "
        "FROM predictions "
        "ORDER BY prediction_date DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_expert_priority_rating(expert_name: str) -> float:
    if not expert_name or expert_name == "Неизвестный эксперт":
        return 50.0
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    rows = conn.execute(
        "SELECT verdict FROM predictions WHERE expert_name = ? AND verdict IN ('confirmed','refuted')",
        (expert_name,)
    ).fetchall()
    conn.close()
    if not rows:
        return 50.0
    return round(sum(1 for r in rows if r[0] == "confirmed") / len(rows) * 100, 1)


# ─── CRUD для редактирования прогнозов ────────────────────

def get_prediction(pred_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, video_id, expert_name, channel_name, prediction_text, "
        "       prediction_date, verdict, verdict_date, verdict_notes "
        "FROM predictions WHERE id = ?", (pred_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_prediction_text(pred_id: int, new_text: str) -> bool:
    new_text = (new_text or "").strip()
    if not new_text or len(new_text) > 400:
        return False
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        cur = conn.execute(
            "UPDATE predictions SET prediction_text = ? WHERE id = ?",
            (new_text, pred_id)
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"⚠️  update_prediction_text: {e}")
        return False
    finally:
        conn.close()


def update_prediction_verdict(pred_id: int, verdict: str, notes: str = "") -> bool:
    if verdict not in ("confirmed", "refuted", "unverifiable", "pending", ""):
        return False
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        if verdict:
            cur = conn.execute(
                "UPDATE predictions SET verdict = ?, verdict_date = CURRENT_TIMESTAMP, "
                "verdict_notes = ? WHERE id = ?",
                (verdict, notes[:500] if notes else None, pred_id)
            )
        else:
            cur = conn.execute(
                "UPDATE predictions SET verdict = NULL, verdict_date = NULL, "
                "verdict_notes = NULL WHERE id = ?",
                (pred_id,)
            )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"⚠️  update_prediction_verdict: {e}")
        return False
    finally:
        conn.close()


def delete_prediction(pred_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        cur = conn.execute("DELETE FROM predictions WHERE id = ?", (pred_id,))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"⚠️  delete_prediction: {e}")
        return False
    finally:
        conn.close()


def list_predictions(
    limit: int = 10,
    offset: int = 0,
    expert: str | None = None,
    verdict: str | None = None,
) -> tuple:
    """Возвращает (rows, total)."""
    where = []
    params = []
    if expert:
        where.append("expert_name = ?")
        params.append(expert)
    if verdict == "pending":
        where.append("(verdict IS NULL OR verdict = '')")
    elif verdict:
        where.append("verdict = ?")
        params.append(verdict)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    total = conn.execute(
        f"SELECT COUNT(*) FROM predictions {where_sql}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, expert_name, channel_name, prediction_text, "
        f"       prediction_date, verdict "
        f"FROM predictions {where_sql} "
        f"ORDER BY prediction_date DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total