from fastapi import APIRouter
from app.core.database import query, state, get_conn
from app.core.class_profile import get_class_profile_json

router = APIRouter(tags=["scene"])


@router.get("/scene")
def get_scene():
    rows = query("SELECT key, value FROM scene")
    meta = {r['key']: r['value'] for r in rows}
    meta['video_available'] = state['video_path'] is not None
    return meta


@router.get("/summary")
def get_summary():
    total = query("SELECT COUNT(*) as n FROM tracks", one=True)['n']
    stationary = query("SELECT COUNT(*) as n FROM tracks WHERE is_stationary=1",
                       one=True)['n']
    classes = query("SELECT class_name, COUNT(*) as cnt FROM tracks "
                    "GROUP BY class_name ORDER BY cnt DESC")
    entry_edges = query("SELECT entry_edge, COUNT(*) as cnt FROM tracks "
                        "GROUP BY entry_edge ORDER BY cnt DESC")
    exit_edges = query("SELECT exit_edge, COUNT(*) as cnt FROM tracks "
                       "GROUP BY exit_edge ORDER BY cnt DESC")
    speeds = query("SELECT speed_mean_px FROM tracks WHERE is_stationary=0 "
                   "AND speed_mean_px > 0")
    speed_values = [s['speed_mean_px'] for s in speeds]
    durations = query("SELECT duration_sec FROM tracks WHERE duration_sec > 0")
    dur_values = [d['duration_sec'] for d in durations]

    return {
        'total_tracks': total,
        'moving_tracks': total - stationary,
        'stationary_tracks': stationary,
        'class_breakdown': {c['class_name']: c['cnt'] for c in classes},
        'entry_edges': {e['entry_edge']: e['cnt'] for e in entry_edges},
        'exit_edges': {e['exit_edge']: e['cnt'] for e in exit_edges},
        'speed_stats': {
            'values': speed_values[:500],
            'mean': sum(speed_values) / len(speed_values) if speed_values else 0,
            'max': max(speed_values) if speed_values else 0,
        },
        'duration_stats': {
            'values': dur_values[:500],
            'mean': sum(dur_values) / len(dur_values) if dur_values else 0,
            'max': max(dur_values) if dur_values else 0,
        },
        'fps': state['fps'],
        'frame_size': [state['frame_width'], state['frame_height']],
    }


@router.get("/class_profile")
def class_profile():
    """
    Return the class profile detected from the .traf file.
    Frontend uses this to dynamically set colors, legends, and column headers.
    """
    return get_class_profile_json(get_conn())
