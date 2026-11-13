"""CRUD для predictions (Postgres)."""
import psycopg2.extras

from db.connection import get_conn


def _save_predictions(video_id, expert_name, channel_name, predictions_list,
                     speaker_id=None):
    if not predictions_list:
        return 0
    if (expert_name or "").strip() in ("Неизвестный спикер", "Неизвестный эксперт", "?"):
        print(f"[SKIP] _save_predictions: expert_name='{expert_name}' — не сохраняю")
        return 0
    conn = get_conn()
    cur = conn.cursor()
    saved = 0
    try:
        for p in predictions_list:
            try:
                cur.execute(
                    "INSERT INTO predictions "
                    "(video_id, expert_name, channel_name, prediction_text, speaker_id) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (video_id, prediction_text) DO NOTHING",
                    (video_id, expert_name, channel_name, p, speaker_id or "")
                )
                if cur.rowcount > 0:
                    saved += 1
            except Exception:
                pass
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return saved


def _recent_predictions(limit=30):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id, expert_name, channel_name, prediction_text, "
            "       prediction_date, verdict, speaker_id, source_url, fact_check_date "
            "FROM predictions ORDER BY prediction_date DESC LIMIT %s", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def get_expert_priority_rating(expert_name: str) -> float:
    if not expert_name or expert_name == "Неизвестный эксперт":
        return 50.0
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT verdict FROM predictions WHERE expert_name=%s AND verdict IN ('confirmed','refuted')",
            (expert_name,)
        )
        rows = cur.fetchall()
        if not rows:
            return 50.0
        return round(sum(1 for r in rows if r[0] == "confirmed") / len(rows) * 100, 1)
    finally:
        cur.close()
        conn.close()


def get_prediction(pred_id: int) -> dict | None:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id, video_id, expert_name, channel_name, prediction_text, "
            "       prediction_date, verdict, verdict_date, verdict_notes, "
            "       speaker_id, source_url, fact_check_date "
            "FROM predictions WHERE id=%s", (pred_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        cur.close()
        conn.close()


def update_prediction_text(pred_id: int, new_text: str) -> bool:
    new_text = (new_text or "").strip()
    if not new_text or len(new_text) > 400:
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE predictions SET prediction_text=%s WHERE id=%s",
                    (new_text, pred_id))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"[WARN]  update_prediction_text: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def update_prediction_verdict(pred_id: int, verdict: str, notes: str = "") -> bool:
    if verdict not in ("confirmed", "refuted", "unverifiable", "pending", ""):
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        if verdict:
            cur.execute("SELECT verdict FROM predictions WHERE id=%s", (pred_id,))
            row = cur.fetchone()
            if row and row[0] and row[0] == verdict:
                return True
            cur.execute(
                "UPDATE predictions SET verdict=%s, verdict_date=NOW(), "
                "verdict_notes=%s WHERE id=%s",
                (verdict, notes[:500] if notes else None, pred_id)
            )
        else:
            cur.execute(
                "UPDATE predictions SET verdict=NULL, verdict_date=NULL, "
                "verdict_notes=NULL WHERE id=%s", (pred_id,)
            )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"[WARN]  update_prediction_verdict: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def delete_prediction(pred_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM predictions WHERE id=%s", (pred_id,))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"[WARN]  delete_prediction: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def list_predictions(limit: int = 10, offset: int = 0,
                     expert: str | None = None, verdict: str | None = None) -> tuple:
    where = []
    params = []
    if expert:
        where.append("expert_name=%s")
        params.append(expert)
    if verdict == "pending":
        where.append("(verdict IS NULL OR verdict='')")
    elif verdict:
        where.append("verdict=%s")
        params.append(verdict)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(f"SELECT COUNT(*) FROM predictions {where_sql}", params)
        total = cur.fetchone()["count"]
        cur.execute(
            f"SELECT id, expert_name, channel_name, prediction_text, "
            f"       prediction_date, verdict, speaker_id, source_url, fact_check_date "
            f"FROM predictions {where_sql} "
            f"ORDER BY prediction_date DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        return [dict(r) for r in cur.fetchall()], total
    finally:
        cur.close()
        conn.close()


def get_predictions_for_factcheck(days: int = 7, limit: int = 10) -> list:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id, video_id, expert_name, channel_name, prediction_text, "
            "       prediction_date, source_url "
            "FROM predictions "
            "WHERE (verdict IS NULL OR verdict='') "
            "  AND prediction_date >= NOW() - make_interval(days => %s) "
            "ORDER BY prediction_date DESC LIMIT %s",
            (days, limit)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def set_fact_check_result(pred_id: int, verdict: str,
                          source_url: str = "", reasoning: str = "") -> bool:
    if verdict not in ("confirmed", "refuted", "unverifiable"):
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        if verdict == "unverifiable":
            cur.execute(
                "UPDATE predictions SET verdict=%s, verdict_date=NOW(), "
                "verdict_notes=%s, fact_check_date=NOW() WHERE id=%s",
                (verdict, (reasoning or "")[:500], pred_id)
            )
        else:
            cur.execute(
                "UPDATE predictions SET verdict=%s, verdict_date=NOW(), "
                "verdict_notes=%s, fact_check_date=NOW(), "
                "source_url=COALESCE(NULLIF(%s,''), source_url) WHERE id=%s",
                (verdict, (reasoning or "")[:500], source_url or "", pred_id)
            )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"[WARN]  set_fact_check_result: {e}")
        return False
    finally:
        cur.close()
        conn.close()
