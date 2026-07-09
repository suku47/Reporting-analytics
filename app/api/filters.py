"""
Track filtering and smoothing endpoints.
"""
from fastapi import APIRouter
from app.core.database import get_conn, state, query
from app.core.track_filter import (
    compute_track_metrics, filter_tracks,
    apply_smoothing_to_db, smooth_trajectory,
    analyze_track_jitter, trim_track_jitter, auto_trim_all_jitter
)
import json

router = APIRouter(prefix="/filters", tags=["filters"])


@router.get("/metrics")
def get_track_metrics():
    """Get filtering metrics for all tracks (displacement, sinuosity, jitter, etc.)."""
    return compute_track_metrics(
        get_conn(), state['frame_width'], state['frame_height'])


@router.post("/apply")
def apply_filters(params: dict = None):
    """
    Compute which tracks to keep/hide based on filter parameters.
    Does NOT delete — returns filter results for the frontend.
    
    Body (all optional):
      min_displacement: float (default 30)
      min_frames: int (default 10)
      max_sinuosity: float (default 5.0)
      max_jitter: float (default 1.2)
      require_gate_crossing: bool (default false)
      remove_edge_only: bool (default true)
    """
    if params is None:
        params = {}

    results = filter_tracks(
        get_conn(), state['frame_width'], state['frame_height'],
        min_displacement=params.get('min_displacement', 30.0),
        min_frames=params.get('min_frames', 10),
        max_sinuosity=params.get('max_sinuosity', 5.0),
        max_jitter=params.get('max_jitter', 1.2),
        require_gate_crossing=params.get('require_gate_crossing', False),
        remove_edge_only=params.get('remove_edge_only', True),
    )

    keep = [tid for tid, r in results.items() if r['keep']]
    remove = {tid: r['reasons'] for tid, r in results.items() if not r['keep']}

    return {
        'total': len(results),
        'keep': len(keep),
        'remove': len(remove),
        'keep_ids': keep,
        'removed': remove,
    }


@router.post("/delete_tracks")
def delete_filtered_tracks(params: dict):
    """
    Permanently delete tracks by ID list.
    Body: { "track_ids": [1, 3, 6] }
    """
    track_ids = params.get('track_ids', [])
    if not track_ids:
        return {'deleted': 0}

    conn = get_conn()
    placeholders = ','.join('?' * len(track_ids))

    conn.execute(f"DELETE FROM observations WHERE track_id IN ({placeholders})", track_ids)
    conn.execute(f"DELETE FROM gate_crossings WHERE track_id IN ({placeholders})", track_ids)
    conn.execute(f"DELETE FROM tracklets WHERE global_track_id IN ({placeholders})", track_ids)
    conn.execute(f"DELETE FROM tracks WHERE track_id IN ({placeholders})", track_ids)
    conn.commit()

    # Update scene metadata
    new_count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    conn.execute("UPDATE scene SET value=? WHERE key='total_vehicles'",
                 (str(new_count),))
    conn.commit()

    return {'deleted': len(track_ids), 'remaining': new_count}


@router.post("/smooth")
def smooth_trajectories(params: dict = None):
    """
    Apply moving-average smoothing to all trajectories.
    Body: { "window": 5 }  (default 5)
    """
    window = (params or {}).get('window', 5)
    updated = apply_smoothing_to_db(get_conn(), window=window)
    return {'smoothed': updated, 'window': window}


# ──────────────────────────────────────────────────────────
# TRAJECTORY TRIMMING
# ──────────────────────────────────────────────────────────

@router.get("/jitter_analysis/{track_id}")
def get_jitter_analysis(track_id: int, threshold: float = 1.0):
    """
    Analyze a single track's trajectory for jittery segments.
    Returns segment breakdown (clean vs jittery regions).
    """
    result = analyze_track_jitter(get_conn(), track_id,
                                  jitter_threshold=threshold)
    if result is None:
        return {'error': 'Track not found'}
    return result


@router.post("/trim_track")
def trim_track(params: dict):
    """
    Trim a track's trajectory to keep only a specific range.
    Body: { "track_id": 42, "keep_start": 5, "keep_end": 180 }
    """
    track_id = params.get('track_id')
    keep_start = params.get('keep_start', 0)
    keep_end = params.get('keep_end')
    if track_id is None or keep_end is None:
        return {'error': 'track_id and keep_end are required'}
    return trim_track_jitter(get_conn(), track_id, keep_start, keep_end)


@router.post("/auto_trim_jitter")
def auto_trim_jitter(params: dict = None):
    """
    Automatically trim jittery ends from ALL trajectories.
    Keeps the longest clean segment of each track.
    Body: { "jitter_threshold": 1.0, "min_clean_length": 5 }
    """
    params = params or {}
    threshold = params.get('jitter_threshold', 1.0)
    min_clean = params.get('min_clean_length', 5)
    return auto_trim_all_jitter(get_conn(), jitter_threshold=threshold,
                                min_clean_length=min_clean)


@router.post("/preview_trim_jitter")
def preview_trim_jitter(params: dict = None):
    """
    Preview what auto-trim would do without applying changes.
    Returns per-track trim analysis.
    """
    params = params or {}
    threshold = params.get('jitter_threshold', 1.0)
    min_clean = params.get('min_clean_length', 5)

    conn = get_conn()
    tracks = conn.execute("SELECT track_id, trajectory_json FROM tracks").fetchall()

    preview = []
    total_would_trim = 0
    total_points_removed = 0

    for t in tracks:
        tid = t[0]
        traj = json.loads(t[1]) if t[1] else []
        if len(traj) < 6:
            continue

        from app.core.track_filter import find_jittery_segments
        segments = find_jittery_segments(traj, jitter_threshold=threshold,
                                         min_clean_length=min_clean)

        # Find longest clean segment
        best_seg = None
        best_len = 0
        for seg in segments:
            if seg['type'] == 'clean':
                seg_len = seg['end'] - seg['start'] + 1
                if seg_len > best_len:
                    best_len = seg_len
                    best_seg = seg

        if best_seg and not (best_seg['start'] == 0 and best_seg['end'] == len(traj) - 1):
            points_removed = len(traj) - best_len
            total_would_trim += 1
            total_points_removed += points_removed
            preview.append({
                'track_id': tid,
                'total_points': len(traj),
                'keep_start': best_seg['start'],
                'keep_end': best_seg['end'],
                'keep_points': best_len,
                'removing_points': points_removed,
                'segments': segments,
            })

    return {
        'total_tracks': len(tracks),
        'would_trim': total_would_trim,
        'unchanged': len(tracks) - total_would_trim,
        'total_points_removed': total_points_removed,
        'details': preview[:200],  # Cap for response size
    }
