"""
Analytics Engine — Speed analysis, OD matrix, near-miss detection.
"""

import json
import sqlite3
import math
from collections import defaultdict


def compute_od_matrix(conn: sqlite3.Connection):
    """
    Origin-Destination matrix: entry_edge → exit_edge counts.
    Returns dict of {(entry, exit): count}.
    """
    rows = conn.execute(
        "SELECT entry_edge, exit_edge, class_name, COUNT(*) as cnt "
        "FROM tracks WHERE is_stationary=0 "
        "GROUP BY entry_edge, exit_edge, class_name"
    ).fetchall()

    matrix = defaultdict(lambda: defaultdict(int))
    matrix_by_class = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    for r in rows:
        entry, exit_, cls, cnt = r[0], r[1], r[2], r[3]
        matrix[entry][exit_] += cnt
        matrix_by_class[cls][entry][exit_] += cnt

    return {
        'total': {k: dict(v) for k, v in matrix.items()},
        'by_class': {cls: {k: dict(v) for k, v in m.items()}
                     for cls, m in matrix_by_class.items()},
        'edges': sorted(set(r[0] for r in rows) | set(r[1] for r in rows)),
    }


def compute_speed_analysis(conn: sqlite3.Connection, fps: float = 30.0):
    """Speed breakdown by class, edge, and time bins."""
    tracks = conn.execute(
        "SELECT class_name, entry_edge, speed_mean_px, speed_max_px, "
        "first_frame, duration_sec FROM tracks "
        "WHERE is_stationary=0 AND speed_mean_px > 0"
    ).fetchall()

    by_class = defaultdict(list)
    by_edge = defaultdict(list)
    time_bins = defaultdict(list)  # 5-minute bins by frame

    for t in tracks:
        cls, edge, mean_s, max_s, ff, dur = t
        by_class[cls].append({'mean': mean_s, 'max': max_s})
        by_edge[edge].append(mean_s)

        # Time bin (every 5 min = 5*60*fps frames)
        bin_size = int(5 * 60 * fps)
        time_bin = (ff // bin_size) * bin_size if bin_size > 0 else 0
        time_bins[time_bin].append(mean_s)

    result = {
        'by_class': {},
        'by_edge': {},
        'time_series': [],
    }

    for cls, speeds in by_class.items():
        means = [s['mean'] for s in speeds]
        result['by_class'][cls] = {
            'count': len(speeds),
            'speed_mean': sum(means) / len(means) if means else 0,
            'speed_max': max(s['max'] for s in speeds) if speeds else 0,
        }

    for edge, speeds in by_edge.items():
        result['by_edge'][edge] = {
            'count': len(speeds),
            'speed_mean': sum(speeds) / len(speeds) if speeds else 0,
        }

    for frame_bin in sorted(time_bins.keys()):
        speeds = time_bins[frame_bin]
        result['time_series'].append({
            'frame': frame_bin,
            'time_sec': frame_bin / fps,
            'count': len(speeds),
            'speed_mean': sum(speeds) / len(speeds),
        })

    return result


def detect_near_misses(conn: sqlite3.Connection, fps: float = 30.0,
                       min_distance_px: float = 50.0,
                       ttc_threshold_sec: float = 2.0):
    """
    Detect near-miss events between simultaneously active tracks.
    
    Uses minimum distance and Time-to-Collision (TTC) metrics.
    Returns list of near-miss events.
    """
    # Get all observations ordered by frame
    tracks_data = {}
    rows = conn.execute(
        "SELECT track_id, frame, cx, cy, speed_px FROM observations "
        "WHERE track_id IN (SELECT track_id FROM tracks WHERE is_stationary=0) "
        "ORDER BY frame"
    ).fetchall()

    for r in rows:
        tid = r[0]
        if tid not in tracks_data:
            tracks_data[tid] = []
        tracks_data[tid].append({
            'frame': r[1], 'cx': r[2], 'cy': r[3], 'speed': r[4] or 0
        })

    # Build frame → active tracks index
    frame_index = defaultdict(list)
    for tid, obs_list in tracks_data.items():
        for obs in obs_list:
            frame_index[obs['frame']].append((tid, obs['cx'], obs['cy'], obs['speed']))

    near_misses = []
    checked_pairs = set()

    # Sample frames (every 3rd frame for performance)
    for frame in sorted(frame_index.keys())[::3]:
        active = frame_index[frame]
        if len(active) < 2:
            continue

        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                tid_a, ax, ay, sa = active[i]
                tid_b, bx, by, sb = active[j]

                pair_key = (min(tid_a, tid_b), max(tid_a, tid_b))
                if pair_key in checked_pairs:
                    continue

                dist = math.sqrt((ax - bx)**2 + (ay - by)**2)

                if dist < min_distance_px:
                    # Compute TTC approximation
                    closing_speed = sa + sb  # simplified
                    ttc = (dist / closing_speed) / fps if closing_speed > 0.1 else 999

                    if ttc < ttc_threshold_sec:
                        near_misses.append({
                            'track_a': tid_a,
                            'track_b': tid_b,
                            'frame': frame,
                            'distance_px': round(dist, 1),
                            'ttc_sec': round(ttc, 2),
                            'position': [round((ax+bx)/2, 1), round((ay+by)/2, 1)],
                            'severity': 'high' if ttc < 0.5 else ('medium' if ttc < 1.0 else 'low'),
                        })
                        checked_pairs.add(pair_key)

    # Sort by severity
    severity_order = {'high': 0, 'medium': 1, 'low': 2}
    near_misses.sort(key=lambda x: (severity_order.get(x['severity'], 3), x['frame']))

    return near_misses[:200]  # Cap results
