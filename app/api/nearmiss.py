"""Near Miss API: detect conflicts on the loaded traf, render the map."""
import os
import tempfile

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(tags=["nearmiss"])
_LAST = {'path': None, 'events': []}


@router.post("/nearmiss/detect")
def detect(payload: dict = None):
    payload = payload or {}
    try:
        from app.core.database import state
        from app.core import near_miss
        traf = state.get('db_path')
        if not traf:
            return {'error': 'No .traf loaded'}
        events = near_miss.detect(
            traf,
            mode=payload.get('mode', 'veh_ped'),
            pet_threshold=float(payload.get('pet_threshold', 3.0)),
            diag_factor=float(payload.get('diag_factor', 0.6)),
            min_angle=float(payload.get('min_angle', 15.0)),
            ttc_threshold=float(payload.get('ttc_threshold', 1.5)),
            enable_ttc=bool(payload.get('enable_ttc', False)))
        _LAST['events'] = events
        out = os.path.join(tempfile.gettempdir(), 'nearmiss_map.png')
        focus = payload.get('focus')
        near_miss.render_conflict_map(traf, events, out,
                                      focus=tuple(focus) if focus else None)
        _LAST['path'] = out
        counts = {}
        for e in events:
            counts[e['severity']] = counts.get(e['severity'], 0) + 1
        dbg_path = os.path.join(os.path.dirname(os.path.abspath(traf)),
                                'nearmiss_debug.log')
        return {'ok': True, 'events': events[:200], 'counts': counts,
                'total': len(events),
                'fps': float(state.get('fps') or 30.0),
                'has_video': bool(state.get('video_path')),
                'debug_log': dbg_path if os.path.exists(dbg_path) else None}
    except FileNotFoundError as e:
        return {'error': str(e)}
    except Exception as e:
        import traceback
        return {'error': str(e), 'trace': traceback.format_exc()[-600:]}


@router.get("/nearmiss/image")
def image():
    p = _LAST.get('path')
    if not p or not os.path.exists(p):
        return {'error': 'Nothing rendered yet'}
    with open(p, 'rb') as f:
        data = f.read()
    return Response(content=data, media_type='image/png',
                    headers={'Cache-Control': 'no-store'})


# ── Annotated conflict clips (trails + bboxes for the pair only) ──────────
CLIP_STATE = {'running': False, 'done': 0, 'total': 0, 'error': None,
              'out_dir': None}


def _find_event(a_id, b_id, frame):
    for e in _LAST.get('events') or []:
        if e['a_id'] == a_id and e['b_id'] == b_id and e['frame'] == frame:
            return e
    return None


@router.post("/nearmiss/clip")
def clip(payload: dict):
    """Render (or reuse) the annotated clip for one conflict; the file is
    stored in <traf_dir>/nearmiss_clips/ and streamed to the browser."""
    try:
        from app.core.database import state
        from app.core import near_miss_clips
        traf = state.get('db_path')
        video = state.get('video_path')
        if not traf:
            return {'error': 'No .traf loaded'}
        ev = _find_event(int(payload['a_id']), int(payload['b_id']),
                         int(payload['frame']))
        if ev is None:
            return {'error': 'Unknown conflict — run Detect Conflicts first'}
        path, codec, cached = near_miss_clips.render_clip(
            traf, video, ev, float(state.get('fps') or 30.0))
        return {'ok': True, 'name': os.path.basename(path),
                'codec': codec, 'cached': cached}
    except FileNotFoundError as e:
        return {'error': str(e)}
    except Exception as e:
        import traceback
        return {'error': str(e), 'trace': traceback.format_exc()[-600:]}


@router.get("/nearmiss/clip_file")
def clip_file(name: str):
    """Serve a rendered clip by basename (restricted to the clips folder)."""
    from app.core.database import state
    from app.core import near_miss_clips
    traf = state.get('db_path')
    if not traf:
        return {'error': 'No .traf loaded'}
    safe = os.path.basename(name)
    p = os.path.join(near_miss_clips.clips_dir(traf), safe)
    if not os.path.exists(p):
        return {'error': f'Clip not found: {safe}'}
    with open(p, 'rb') as f:
        data = f.read()
    return Response(content=data, media_type='video/mp4',
                    headers={'Cache-Control': 'no-store',
                             'Content-Length': str(len(data)),
                             'Accept-Ranges': 'bytes'})


@router.post("/nearmiss/export_all")
def export_all():
    """Render every detected conflict's clip to disk (background thread) —
    same output the old near_miss_analyzer.py run produced."""
    import threading
    from app.core.database import state
    from app.core import near_miss_clips
    if CLIP_STATE['running']:
        return {'error': 'Export already in progress'}
    events = _LAST.get('events') or []
    traf = state.get('db_path')
    video = state.get('video_path')
    if not traf or not events:
        return {'error': 'Run Detect Conflicts first'}
    if not video:
        return {'error': 'No video loaded — start the viewer with --video'}
    fps = float(state.get('fps') or 30.0)
    CLIP_STATE.update({'running': True, 'done': 0, 'total': len(events),
                       'error': None,
                       'out_dir': near_miss_clips.clips_dir(traf)})

    def _run():
        try:
            for ev in events:
                near_miss_clips.render_clip(traf, video, ev, fps)
                CLIP_STATE['done'] += 1
        except Exception as e:
            CLIP_STATE['error'] = str(e)
        finally:
            CLIP_STATE['running'] = False

    threading.Thread(target=_run, daemon=True).start()
    return {'started': True, 'total': len(events),
            'out_dir': CLIP_STATE['out_dir']}


@router.get("/nearmiss/clip_status")
def clip_status():
    return dict(CLIP_STATE)
