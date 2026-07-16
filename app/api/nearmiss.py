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
            ttc_threshold=float(payload.get('ttc_threshold', 1.5)))
        _LAST['events'] = events
        out = os.path.join(tempfile.gettempdir(), 'nearmiss_map.png')
        focus = payload.get('focus')
        near_miss.render_conflict_map(traf, events, out,
                                      focus=tuple(focus) if focus else None)
        _LAST['path'] = out
        counts = {}
        for e in events:
            counts[e['severity']] = counts.get(e['severity'], 0) + 1
        return {'ok': True, 'events': events[:200], 'counts': counts,
                'total': len(events)}
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
