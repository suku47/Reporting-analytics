import json
from fastapi import APIRouter, HTTPException
from app.core.database import query

router = APIRouter(tags=["tracks"])


@router.get("/tracks")
def get_tracks(class_name: str = None, stationary: int = None,
               min_quality: float = None, entry_edge: str = None,
               exit_edge: str = None, limit: int = 200000):
    sql = "SELECT * FROM tracks WHERE 1=1"
    params = []
    if class_name:
        sql += " AND class_name=?"; params.append(class_name)
    if stationary is not None:
        sql += " AND is_stationary=?"; params.append(stationary)
    if min_quality is not None:
        sql += " AND track_quality>=?"; params.append(min_quality)
    if entry_edge:
        sql += " AND entry_edge=?"; params.append(entry_edge)
    if exit_edge:
        sql += " AND exit_edge=?"; params.append(exit_edge)
    sql += f" ORDER BY track_id LIMIT {limit}"
    return query(sql, params)


@router.get("/tracks/{track_id}")
def get_track(track_id: int):
    track = query("SELECT * FROM tracks WHERE track_id=?", (track_id,), one=True)
    if not track:
        raise HTTPException(404, "Track not found")
    return track


@router.get("/tracks/{track_id}/observations")
def get_track_observations(track_id: int, sample: int = None):
    obs = query("SELECT * FROM observations WHERE track_id=? ORDER BY frame",
                (track_id,))
    if sample and len(obs) > sample:
        step = max(1, len(obs) // sample)
        return obs[::step]
    return obs


@router.get("/trajectories")
def get_all_trajectories(class_name: str = None, stationary: int = None):
    sql = ("SELECT track_id, class_name, is_stationary, trajectory_json, "
           "speed_mean_px, entry_edge, exit_edge FROM tracks WHERE 1=1")
    params = []
    if class_name:
        sql += " AND class_name=?"; params.append(class_name)
    if stationary is not None:
        sql += " AND is_stationary=?"; params.append(stationary)
    tracks = query(sql, params)
    for t in tracks:
        t['trajectory'] = json.loads(t['trajectory_json']) if t['trajectory_json'] else []
        del t['trajectory_json']
    return tracks


@router.get("/tracklets")
def get_tracklets(global_track_id: int = None):
    if global_track_id is not None:
        return query("SELECT * FROM tracklets WHERE global_track_id=?",
                     (global_track_id,))
    return query("SELECT * FROM tracklets LIMIT 2000")
