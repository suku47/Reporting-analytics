"""
Gate Engine - Compute which tracks cross virtual gate lines.

v2: Uses full-resolution observations, populates timestamps/speed,
    reads excluded classes dynamically from .traf class profile.
v3: Records ALL crossings per track per gate (not just the first), so
    U-turns (same leg crossed inbound then outbound) become visible.
    A time debounce (MIN_RECROSS_SEC) suppresses jitter double-fires
    when a vehicle creeps back and forth across the gate line.
"""

import json
import sqlite3
from datetime import datetime, timedelta

ENGINE_VERSION = '3'
MIN_RECROSS_SEC = 1.5   # ignore re-crossings of the same gate within this window


def cross_product(ax, ay, bx, by, px, py):
    return (bx - ax) * (py - ay) - (by - ay) * (px - ax)


def segments_intersect(ax, ay, bx, by, cx, cy, dx, dy):
    d1 = cross_product(cx, cy, dx, dy, ax, ay)
    d2 = cross_product(cx, cy, dx, dy, bx, by)
    d3 = cross_product(ax, ay, bx, by, cx, cy)
    d4 = cross_product(ax, ay, bx, by, dx, dy)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def _get_excluded_classes(conn):
    try:
        from app.core.class_profile import get_class_profile
        profile = get_class_profile(conn)
        return profile['excluded_classes']
    except Exception:
        return {'Peds', 'Pedestrian', 'Ped', 'Cyclist', 'Bicycle'}


def _get_scene_time_info(conn):
    meta = {}
    try:
        for r in conn.execute("SELECT key, value FROM scene"):
            meta[r[0]] = r[1]
    except Exception:
        pass
    fps = float(meta.get('fps', 30.0))
    start_time = None
    raw = meta.get('video_start_time')
    if raw and raw not in ('None', 'null', ''):
        try:
            start_time = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            pass
    return start_time, fps


def _frame_to_timestamp(frame, start_time, fps):
    if start_time is None or fps <= 0:
        return None
    return (start_time + timedelta(seconds=frame / fps)).isoformat()


def compute_gate_crossings(conn, gate_id, use_full_resolution=True):
    gate = conn.execute("SELECT * FROM gates WHERE gate_id=?", (gate_id,)).fetchone()
    if not gate:
        return 0
    gx1, gy1, gx2, gy2 = gate[2], gate[3], gate[4], gate[5]
    direction = gate[6] or 'both'
    start_time, fps = _get_scene_time_info(conn)
    excluded = _get_excluded_classes(conn)

    if use_full_resolution:
        crossings = _from_observations(conn, gate_id, gx1, gy1, gx2, gy2, direction, start_time, fps, excluded)
    else:
        crossings = _from_trajectory(conn, gate_id, gx1, gy1, gx2, gy2, direction, start_time, fps, excluded)

    conn.execute("DELETE FROM gate_crossings WHERE gate_id=?", (gate_id,))
    if crossings:
        conn.executemany(
            "INSERT INTO gate_crossings "
            "(gate_id, track_id, frame, timestamp, direction, speed_px, class_id, class_name) "
            "VALUES (?,?,?,?,?,?,?,?)", crossings)
    conn.execute("INSERT OR REPLACE INTO scene (key, value) VALUES ('crossings_engine_version', ?)",
                 (ENGINE_VERSION,))
    conn.commit()
    return len(crossings)


def _from_observations(conn, gate_id, gx1, gy1, gx2, gy2, direction, start_time, fps, excluded):
    if excluded:
        ph = ','.join('?' * len(excluded))
        tracks = conn.execute(
            "SELECT track_id, class_id, class_name FROM tracks "
            "WHERE is_stationary=0 AND class_name NOT IN (%s)" % ph,
            tuple(excluded)).fetchall()
    else:
        tracks = conn.execute(
            "SELECT track_id, class_id, class_name FROM tracks WHERE is_stationary=0").fetchall()

    crossings = []
    debounce_frames = MIN_RECROSS_SEC * fps if fps > 0 else 45
    for t in tracks:
        track_id, class_id, class_name = t[0], t[1], t[2]
        obs = conn.execute(
            "SELECT frame, cx, cy, speed_px FROM observations WHERE track_id=? ORDER BY frame",
            (track_id,)).fetchall()
        if len(obs) < 2:
            continue
        last_cross_frame = None
        for i in range(len(obs) - 1):
            f1, cx1, cy1, _ = obs[i]
            f2, cx2, cy2, speed = obs[i + 1]
            if segments_intersect(cx1, cy1, cx2, cy2, gx1, gy1, gx2, gy2):
                if last_cross_frame is not None and (f2 - last_cross_frame) < debounce_frames:
                    last_cross_frame = f2  # jitter re-cross: suppress but keep window rolling
                    continue
                cross = cross_product(gx1, gy1, gx2, gy2, cx2, cy2)
                cross_dir = 'positive' if cross > 0 else 'negative'
                if direction == 'both' or direction == cross_dir:
                    ts = _frame_to_timestamp(f2, start_time, fps)
                    crossings.append((gate_id, track_id, f2, ts, cross_dir,
                                      round(speed or 0.0, 2), class_id, class_name))
                    last_cross_frame = f2
    return crossings


def _from_trajectory(conn, gate_id, gx1, gy1, gx2, gy2, direction, start_time, fps, excluded):
    if excluded:
        ph = ','.join('?' * len(excluded))
        tracks = conn.execute(
            "SELECT track_id, class_id, class_name, trajectory_json FROM tracks "
            "WHERE is_stationary=0 AND class_name NOT IN (%s)" % ph,
            tuple(excluded)).fetchall()
    else:
        tracks = conn.execute(
            "SELECT track_id, class_id, class_name, trajectory_json FROM tracks "
            "WHERE is_stationary=0").fetchall()

    crossings = []
    debounce_frames = MIN_RECROSS_SEC * fps if fps > 0 else 45
    for t in tracks:
        traj = json.loads(t[3]) if t[3] else []
        if len(traj) < 2:
            continue
        last_cross_frame = None
        for i in range(len(traj) - 1):
            px1, py1 = traj[i][0], traj[i][1]
            px2, py2 = traj[i + 1][0], traj[i + 1][1]
            frame = traj[i + 1][2] if len(traj[i + 1]) > 2 else i + 1
            if segments_intersect(px1, py1, px2, py2, gx1, gy1, gx2, gy2):
                if last_cross_frame is not None and (frame - last_cross_frame) < debounce_frames:
                    last_cross_frame = frame
                    continue
                cross = cross_product(gx1, gy1, gx2, gy2, px2, py2)
                cross_dir = 'positive' if cross > 0 else 'negative'
                if direction == 'both' or direction == cross_dir:
                    ts = _frame_to_timestamp(frame, start_time, fps)
                    crossings.append((gate_id, t[0], frame, ts, cross_dir, 0.0, t[1], t[2]))
                    last_cross_frame = frame
    return crossings


def recompute_all_gates(conn, use_full_resolution=True):
    gates = conn.execute("SELECT gate_id FROM gates").fetchall()
    total = 0
    for g in gates:
        total += compute_gate_crossings(conn, g[0], use_full_resolution)
    return total
