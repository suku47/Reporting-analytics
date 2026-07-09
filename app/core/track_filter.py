"""
Track Filtering & Trajectory Smoothing

Filters:
  1. Min gate crossings: Remove tracks that don't cross at least N gates
  2. Min trajectory length: Remove very short tracks (few frames)
  3. Min displacement: Remove tracks that don't travel far enough (stuck detections)
  4. Edge rejection: Remove tracks that start AND end at frame edges (partial tracks)
  5. Trajectory smoothing: Moving average to reduce jitter/zigzag

All filters are non-destructive — they mark tracks as filtered but don't delete data.
"""

import json
import math
import sqlite3


def compute_track_metrics(conn: sqlite3.Connection, frame_width: int, frame_height: int):
    """
    Compute filtering metrics for all tracks.
    Returns list of dicts with track_id + computed metrics.
    """
    tracks = conn.execute(
        "SELECT track_id, entry_x, entry_y, exit_x, exit_y, "
        "total_frames, is_stationary, speed_mean_px, trajectory_json, "
        "class_name, bbox_diag_mean "
        "FROM tracks"
    ).fetchall()

    edge_margin = 0.03  # 3% of frame = edge zone
    ex = frame_width * edge_margin
    ey = frame_height * edge_margin

    results = []
    for t in tracks:
        tid = t[0]
        entry_x, entry_y = t[1] or 0, t[2] or 0
        exit_x, exit_y = t[3] or 0, t[4] or 0
        total_frames = t[5] or 0
        is_stat = t[6]
        speed = t[7] or 0
        traj_json = t[8]
        cls = t[9]
        bbox_diag = t[10] or 0

        # Parse trajectory
        traj = json.loads(traj_json) if traj_json else []

        # Displacement (straight-line distance entry to exit)
        displacement = math.sqrt((exit_x - entry_x)**2 + (exit_y - entry_y)**2)

        # Path length (sum of all segment lengths)
        path_length = 0
        for i in range(1, len(traj)):
            dx = traj[i][0] - traj[i-1][0]
            dy = traj[i][1] - traj[i-1][1]
            path_length += math.sqrt(dx*dx + dy*dy)

        # Sinuosity (path_length / displacement) — 1.0 = straight, >2 = very winding
        sinuosity = (path_length / displacement) if displacement > 1 else 999

        # Edge detection
        entry_at_edge = (entry_x < ex or entry_x > frame_width - ex or
                         entry_y < ey or entry_y > frame_height - ey)
        exit_at_edge = (exit_x < ex or exit_x > frame_width - ex or
                        exit_y < ey or exit_y > frame_height - ey)

        # Jitter score: average frame-to-frame direction change
        jitter = 0
        if len(traj) >= 3:
            angle_changes = []
            for i in range(2, len(traj)):
                dx1 = traj[i-1][0] - traj[i-2][0]
                dy1 = traj[i-1][1] - traj[i-2][1]
                dx2 = traj[i][0] - traj[i-1][0]
                dy2 = traj[i][1] - traj[i-1][1]
                len1 = math.sqrt(dx1*dx1 + dy1*dy1)
                len2 = math.sqrt(dx2*dx2 + dy2*dy2)
                if len1 > 0.5 and len2 > 0.5:
                    cos_a = max(-1, min(1, (dx1*dx2 + dy1*dy2) / (len1 * len2)))
                    angle_changes.append(abs(math.acos(cos_a)))
            jitter = sum(angle_changes) / len(angle_changes) if angle_changes else 0

        # Gate crossings count
        gate_count = conn.execute(
            "SELECT COUNT(DISTINCT gate_id) FROM gate_crossings WHERE track_id=?",
            (tid,)).fetchone()[0]

        results.append({
            'track_id': tid,
            'class_name': cls,
            'total_frames': total_frames,
            'displacement': round(displacement, 1),
            'path_length': round(path_length, 1),
            'sinuosity': round(sinuosity, 2),
            'speed_mean': round(speed, 2),
            'is_stationary': is_stat,
            'entry_at_edge': entry_at_edge,
            'exit_at_edge': exit_at_edge,
            'jitter': round(jitter, 3),
            'gate_crossings': gate_count,
            'bbox_diag_mean': round(bbox_diag, 1),
        })

    return results


def filter_tracks(conn: sqlite3.Connection, frame_width: int, frame_height: int,
                  min_displacement: float = 30.0,
                  min_frames: int = 10,
                  max_sinuosity: float = 5.0,
                  max_jitter: float = 1.2,
                  require_gate_crossing: bool = False,
                  remove_edge_only: bool = True):
    """
    Determine which tracks should be kept vs filtered.
    Returns dict of {track_id: {'keep': bool, 'reasons': [str]}}.
    """
    metrics = compute_track_metrics(conn, frame_width, frame_height)
    results = {}

    for m in metrics:
        tid = m['track_id']
        keep = True
        reasons = []

        # Skip stationary — different rules
        if m['is_stationary']:
            results[tid] = {'keep': True, 'reasons': ['stationary']}
            continue

        # Min frames
        if m['total_frames'] < min_frames:
            keep = False
            reasons.append(f"too_short ({m['total_frames']} frames)")

        # Min displacement
        if m['displacement'] < min_displacement:
            keep = False
            reasons.append(f"low_displacement ({m['displacement']}px)")

        # Sinuosity (too winding = erratic)
        if m['sinuosity'] > max_sinuosity:
            keep = False
            reasons.append(f"erratic_path (sinuosity={m['sinuosity']})")

        # Jitter (too much direction change = noisy detection)
        if m['jitter'] > max_jitter:
            keep = False
            reasons.append(f"high_jitter ({m['jitter']:.2f} rad)")

        # Edge-only tracks (enter AND exit at edge without crossing scene)
        if remove_edge_only and m['entry_at_edge'] and m['exit_at_edge']:
            if m['displacement'] < frame_width * 0.15:
                keep = False
                reasons.append("edge_only")

        # Gate crossing requirement
        if require_gate_crossing and m['gate_crossings'] == 0:
            keep = False
            reasons.append("no_gate_crossing")

        results[tid] = {'keep': keep, 'reasons': reasons}

    return results


def smooth_trajectory(trajectory, window=5):
    """
    Apply moving average smoothing to a trajectory.
    trajectory: list of [x, y, frame] points
    Returns smoothed trajectory (same format).
    """
    if len(trajectory) < window:
        return trajectory

    smoothed = []
    half = window // 2

    for i in range(len(trajectory)):
        start = max(0, i - half)
        end = min(len(trajectory), i + half + 1)
        sx = sum(p[0] for p in trajectory[start:end]) / (end - start)
        sy = sum(p[1] for p in trajectory[start:end]) / (end - start)
        frame = trajectory[i][2] if len(trajectory[i]) > 2 else i
        smoothed.append([round(sx, 1), round(sy, 1), frame])

    return smoothed


def apply_smoothing_to_db(conn: sqlite3.Connection, window=5):
    """Smooth all trajectories in the database."""
    tracks = conn.execute("SELECT track_id, trajectory_json FROM tracks").fetchall()
    updated = 0

    for t in tracks:
        traj = json.loads(t[1]) if t[1] else []
        if len(traj) < window:
            continue
        smoothed = smooth_trajectory(traj, window)
        conn.execute("UPDATE tracks SET trajectory_json=? WHERE track_id=?",
                     (json.dumps(smoothed), t[0]))
        updated += 1

    conn.commit()
    return updated


# ──────────────────────────────────────────────────────────
# TRAJECTORY TRIMMING — remove jittery segments, keep clean parts
# ──────────────────────────────────────────────────────────

def _compute_point_jitter(traj, i):
    """Compute direction change angle at point i (in radians). 0 = straight."""
    if i < 1 or i >= len(traj) - 1:
        return 0.0
    dx1 = traj[i][0] - traj[i-1][0]
    dy1 = traj[i][1] - traj[i-1][1]
    dx2 = traj[i+1][0] - traj[i][0]
    dy2 = traj[i+1][1] - traj[i][1]
    len1 = math.sqrt(dx1*dx1 + dy1*dy1)
    len2 = math.sqrt(dx2*dx2 + dy2*dy2)
    if len1 < 0.5 or len2 < 0.5:
        return math.pi  # near-zero movement = maximally jittery
    cos_a = max(-1, min(1, (dx1*dx2 + dy1*dy2) / (len1 * len2)))
    return abs(math.acos(cos_a))


def find_jittery_segments(traj, jitter_threshold=1.0, min_clean_length=5):
    """
    Analyze a trajectory and find jittery vs clean segments.

    Returns list of segments: [{'start': i, 'end': j, 'type': 'clean'|'jittery'}, ...]

    Logic: scan through trajectory, compute per-point jitter (direction change angle).
    Use a sliding window to classify regions as jittery or clean.
    """
    n = len(traj)
    if n < 4:
        return [{'start': 0, 'end': n - 1, 'type': 'clean'}]

    # Compute per-point jitter scores
    jitter_scores = [0.0] * n
    for i in range(1, n - 1):
        jitter_scores[i] = _compute_point_jitter(traj, i)

    # Smooth jitter scores with a small window to avoid single-point noise
    window = 3
    smoothed_jitter = [0.0] * n
    for i in range(n):
        start = max(0, i - window // 2)
        end = min(n, i + window // 2 + 1)
        smoothed_jitter[i] = sum(jitter_scores[start:end]) / (end - start)

    # Classify each point as clean or jittery
    is_jittery = [s > jitter_threshold for s in smoothed_jitter]

    # Build segments of consecutive clean/jittery points
    segments = []
    seg_start = 0
    seg_type = 'jittery' if is_jittery[0] else 'clean'

    for i in range(1, n):
        curr_type = 'jittery' if is_jittery[i] else 'clean'
        if curr_type != seg_type:
            segments.append({'start': seg_start, 'end': i - 1, 'type': seg_type})
            seg_start = i
            seg_type = curr_type
    segments.append({'start': seg_start, 'end': n - 1, 'type': seg_type})

    # Merge very short clean segments surrounded by jittery into jittery
    merged = []
    for seg in segments:
        seg_len = seg['end'] - seg['start'] + 1
        if seg['type'] == 'clean' and seg_len < min_clean_length:
            seg['type'] = 'jittery'
        merged.append(seg)

    # Re-merge adjacent same-type segments
    final = [merged[0]]
    for seg in merged[1:]:
        if seg['type'] == final[-1]['type']:
            final[-1]['end'] = seg['end']
        else:
            final.append(seg)

    return final


def analyze_track_jitter(conn: sqlite3.Connection, track_id: int,
                         jitter_threshold=1.0):
    """
    Analyze a single track and return its jittery/clean segments.
    Returns dict with trajectory points and segment analysis.
    """
    row = conn.execute(
        "SELECT trajectory_json FROM tracks WHERE track_id=?",
        (track_id,)).fetchone()
    if not row or not row[0]:
        return None

    traj = json.loads(row[0])
    segments = find_jittery_segments(traj, jitter_threshold=jitter_threshold)

    return {
        'track_id': track_id,
        'total_points': len(traj),
        'segments': segments,
        'trajectory': traj,
    }


def trim_track_jitter(conn: sqlite3.Connection, track_id: int,
                      keep_start: int, keep_end: int):
    """
    Trim a track's trajectory to only keep points from keep_start to keep_end (inclusive).
    Updates both the trajectory_json in tracks and removes observations outside the range.

    Args:
        conn: database connection
        track_id: track to trim
        keep_start: first point index to keep (0-based into trajectory)
        keep_end: last point index to keep (0-based into trajectory)

    Returns:
        dict with old_points, new_points counts
    """
    row = conn.execute(
        "SELECT trajectory_json FROM tracks WHERE track_id=?",
        (track_id,)).fetchone()
    if not row or not row[0]:
        return {'error': 'Track not found'}

    traj = json.loads(row[0])
    old_count = len(traj)

    if keep_start < 0:
        keep_start = 0
    if keep_end >= len(traj):
        keep_end = len(traj) - 1
    if keep_start >= keep_end:
        return {'error': 'Invalid range — nothing to keep'}

    # Trim trajectory
    new_traj = traj[keep_start:keep_end + 1]
    new_count = len(new_traj)

    # Get frame numbers for the kept range (for trimming observations)
    kept_frames = set()
    for pt in new_traj:
        if len(pt) > 2:
            kept_frames.add(int(pt[2]))

    # Update trajectory_json
    conn.execute("UPDATE tracks SET trajectory_json=? WHERE track_id=?",
                 (json.dumps(new_traj), track_id))

    # Trim observations to match (if we have frame info)
    obs_deleted = 0
    if kept_frames:
        # Get all observation frames for this track
        all_obs_frames = [r[0] for r in conn.execute(
            "SELECT frame FROM observations WHERE track_id=? ORDER BY frame",
            (track_id,)).fetchall()]

        if all_obs_frames:
            min_keep_frame = min(kept_frames)
            max_keep_frame = max(kept_frames)

            # Delete observations outside the kept frame range
            obs_deleted = conn.execute(
                "DELETE FROM observations WHERE track_id=? AND (frame < ? OR frame > ?)",
                (track_id, min_keep_frame, max_keep_frame)).rowcount

            # Update track metadata
            remaining_obs = conn.execute(
                "SELECT COUNT(*), MIN(frame), MAX(frame) FROM observations WHERE track_id=?",
                (track_id,)).fetchone()
            if remaining_obs and remaining_obs[0] > 0:
                new_total = remaining_obs[0]
                new_first = remaining_obs[1]
                new_last = remaining_obs[2]

                # Recompute entry/exit from new trajectory
                entry_x, entry_y = new_traj[0][0], new_traj[0][1]
                exit_x, exit_y = new_traj[-1][0], new_traj[-1][1]

                conn.execute(
                    "UPDATE tracks SET total_frames=?, first_frame=?, last_frame=?, "
                    "entry_x=?, entry_y=?, exit_x=?, exit_y=?, "
                    "duration_sec=?, observed_frames=? "
                    "WHERE track_id=?",
                    (new_total, new_first, new_last,
                     round(entry_x, 1), round(entry_y, 1),
                     round(exit_x, 1), round(exit_y, 1),
                     None, new_total, track_id))

    conn.commit()

    return {
        'track_id': track_id,
        'old_points': old_count,
        'new_points': new_count,
        'observations_removed': obs_deleted,
        'kept_range': [keep_start, keep_end],
    }


def auto_trim_all_jitter(conn: sqlite3.Connection, jitter_threshold=1.0,
                         min_clean_length=5):
    """
    Automatically trim jittery ends from ALL trajectories.
    Keeps the longest clean segment of each track.

    Returns summary of changes.
    """
    tracks = conn.execute("SELECT track_id, trajectory_json FROM tracks").fetchall()
    trimmed = 0
    total_points_removed = 0

    for t in tracks:
        tid = t[0]
        traj = json.loads(t[1]) if t[1] else []
        if len(traj) < 6:
            continue

        segments = find_jittery_segments(traj, jitter_threshold=jitter_threshold,
                                         min_clean_length=min_clean_length)

        # Find the longest clean segment
        best_seg = None
        best_len = 0
        for seg in segments:
            if seg['type'] == 'clean':
                seg_len = seg['end'] - seg['start'] + 1
                if seg_len > best_len:
                    best_len = seg_len
                    best_seg = seg

        if best_seg is None:
            continue

        # Only trim if we're actually removing something
        if best_seg['start'] == 0 and best_seg['end'] == len(traj) - 1:
            continue  # Already fully clean

        old_count = len(traj)
        result = trim_track_jitter(conn, tid, best_seg['start'], best_seg['end'])
        if 'error' not in result:
            trimmed += 1
            total_points_removed += old_count - result['new_points']

    return {
        'tracks_trimmed': trimmed,
        'tracks_unchanged': len(tracks) - trimmed,
        'total_points_removed': total_points_removed,
    }
