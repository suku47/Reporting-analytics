from datetime import datetime
from fastapi import APIRouter
from app.core.database import query, get_conn
from app.core.gate_engine import compute_gate_crossings

router = APIRouter(tags=["gates"])

ARM_MODES = ('two', 'in', 'out')


def _ensure_arm_mode(conn):
    """Add the arm_mode column to older .traf files (default: two-way)."""
    try:
        conn.execute("ALTER TABLE gates ADD COLUMN arm_mode TEXT DEFAULT 'two'")
        conn.commit()
    except Exception:
        pass  # column already exists


@router.get("/gates")
def list_gates():
    _ensure_arm_mode(get_conn())
    return query("SELECT * FROM gates")


# IMPORTANT: /gates/count_summary MUST be before /gates/{gate_id}
# otherwise FastAPI matches "count_summary" as a gate_id
@router.get("/gates/count_summary")
def gate_count_summary():
    _ensure_arm_mode(get_conn())
    gates_list = query("SELECT * FROM gates")
    for g in gates_list:
        crossings = query(
            "SELECT gc.class_name, gc.direction, COUNT(*) as cnt "
            "FROM gate_crossings gc "
            "WHERE gc.gate_id=? GROUP BY gc.class_name, gc.direction",
            (g['gate_id'],))
        g['counts'] = crossings
        g['total'] = sum(c['cnt'] for c in crossings)
    return gates_list


@router.delete("/gates/all")
def delete_all_gates():
    """Delete all gates and their crossings."""
    conn = get_conn()
    conn.execute("DELETE FROM gate_crossings")
    conn.execute("DELETE FROM gates")
    conn.commit()
    return {'deleted': 'all'}


@router.post("/gates")
def create_gate(gate: dict):
    conn = get_conn()
    _ensure_arm_mode(conn)
    mode = gate.get('arm_mode', 'two')
    if mode not in ARM_MODES:
        mode = 'two'
    conn.execute(
        "INSERT INTO gates (name, x1, y1, x2, y2, direction, approach, "
        "created_at, arm_mode) VALUES (?,?,?,?,?,?,?,?,?)",
        (gate['name'], gate['x1'], gate['y1'], gate['x2'], gate['y2'],
         gate.get('direction', 'both'), gate.get('approach'),
         datetime.now().isoformat(), mode))
    conn.commit()
    gate_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    crossings = compute_gate_crossings(conn, gate_id)
    return {'gate_id': gate_id, 'crossings': crossings}


@router.patch("/gates/{gate_id}/arm_mode")
def set_gate_arm_mode(gate_id: int, payload: dict):
    """Set the arm's one-way mode: 'two' (two-way), 'in' (one-way towards
    junction: approach only), 'out' (one-way away: exit only). Used by the
    batch auto-movement numbering."""
    mode = str(payload.get('arm_mode', '')).strip().lower()
    if mode not in ARM_MODES:
        return {'error': f"arm_mode must be one of {ARM_MODES}"}
    conn = get_conn()
    _ensure_arm_mode(conn)
    conn.execute("UPDATE gates SET arm_mode=? WHERE gate_id=?", (mode, gate_id))
    conn.commit()
    return {'gate_id': gate_id, 'arm_mode': mode}


@router.delete("/gates/{gate_id}")
def delete_gate(gate_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM gate_crossings WHERE gate_id=?", (gate_id,))
    conn.execute("DELETE FROM gates WHERE gate_id=?", (gate_id,))
    conn.commit()
    return {'deleted': gate_id}


@router.get("/gates/{gate_id}/crossings")
def get_gate_crossings(gate_id: int):
    return query(
        "SELECT gc.*, t.class_name FROM gate_crossings gc "
        "JOIN tracks t ON gc.track_id = t.track_id "
        "WHERE gc.gate_id=? ORDER BY gc.frame", (gate_id,))
