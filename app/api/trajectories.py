"""Trajectory analysis API: render all-track trajectory plots from the
currently loaded .traf + video, with class filters and styling options."""

import os
import time

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["trajectories"])

_LAST_RENDER = {'path': None}


@router.post("/trajectories/render")
def render(payload: dict = None):
    """Render a trajectory plot. payload (all optional):
    { classes: ["Car","Ped"], per_class: 50, thickness: 1, legend: true,
      skip_stationary: true, frame: 0, job_number: "...", site_name: "..." }
    Returns metadata; fetch the image via GET /trajectories/image."""
    payload = payload or {}
    try:
        from app.core.database import state
        from app.core.trajectory_render import render_trajectory_plot

        traf = state.get('db_path')
        video = state.get('video_path')
        if not traf:
            return {'error': 'No .traf loaded'}

        # Background: clean zero-detection frame (auto-stored in the .traf),
        # so the video is only needed the first time — after that the .traf
        # is self-contained.
        from app.core.background_frame import resolve_background
        try:
            bg_canvas, bg_desc = resolve_background(
                traf, video, frame=payload.get('frame', 'auto'))
        except FileNotFoundError as e:
            return {'error': str(e)}

        if payload.get('save', True):
            out_dir = os.path.join(os.path.dirname(traf), 'trajectory_plots')
        else:
            import tempfile
            out_dir = os.path.join(tempfile.gettempdir(), 'traj_preview')
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(traf))[0]
        out_path = os.path.join(out_dir, f"{stem}_trajectories_{int(time.time())}.png")

        # Gate-pair filter: tracks that crossed from_gate then to_gate
        track_ids = None
        fg, tg = payload.get('from_gate'), payload.get('to_gate')
        if fg and tg:
            import sqlite3
            c = sqlite3.connect(traf)
            rows = c.execute(
                """SELECT a.track_id FROM
                     (SELECT track_id, MIN(frame) f FROM gate_crossings
                      WHERE gate_id=? GROUP BY track_id) a
                   JOIN
                     (SELECT track_id, MAX(frame) f FROM gate_crossings
                      WHERE gate_id=? GROUP BY track_id) b
                   ON a.track_id = b.track_id WHERE a.f < b.f""",
                (int(fg), int(tg))).fetchall()
            c.close()
            track_ids = [r[0] for r in rows]
            if not track_ids:
                return {'error': 'No tracks travel between those two gates '
                                 '(in that order). Try swapping From/To.'}

        # Clock-time window → frame window
        frame_from = frame_to = None
        t_from, t_to = payload.get('from_time'), payload.get('to_time')
        if t_from or t_to:
            from datetime import datetime
            import sqlite3
            c = sqlite3.connect(traf)
            meta = dict(c.execute("SELECT key, value FROM scene"))
            c.close()
            try:
                vs = datetime.fromisoformat(meta['video_start_time'])
                fps = float(meta.get('fps', 30.0))
            except (KeyError, ValueError):
                return {'error': 'This .traf has no video_start_time — '
                                 'time filtering unavailable.'}
            def _to_frame(hhmm):
                h, m = (int(x) for x in hhmm.split(':'))
                target = vs.replace(hour=h, minute=m, second=0)
                return int((target - vs).total_seconds() * fps)
            if t_from:
                frame_from = max(0, _to_frame(t_from))
            if t_to:
                frame_to = _to_frame(t_to)
            if frame_to is not None and frame_to <= 0:
                return {'error': f'Time window ends before the video starts '
                                 f'(video begins {vs.strftime("%H:%M:%S")}).'}

        result = render_trajectory_plot(
            traf, video, out_path,
            classes=payload.get('classes') or None,
            per_class=payload.get('per_class'),
            thickness=int(payload.get('thickness', 1)),
            legend=bool(payload.get('legend', True)),
            legend_counts=bool(payload.get('legend_counts', True)),
            skip_stationary=bool(payload.get('skip_stationary', True)),
            keep_frac=float(payload.get('keep_frac', 0.5)),
            background=bg_canvas,
            job_number=payload.get('job_number'),
            site_name=payload.get('site_name'),
            track_ids=track_ids,
            frame_from=frame_from, frame_to=frame_to,
        )
        _LAST_RENDER['path'] = result['out_path']
        return {'ok': True,
                'background': bg_desc,
                'out_path': result['out_path'],
                'file_class_counts': result['file_class_counts'],
                'drawn_by_class': result['drawn_by_class'],
                'saved': bool(payload.get('save', True))}
    except ValueError as e:
        return {'error': str(e)}
    except Exception as e:
        import traceback
        return {'error': f"{e}", 'trace': traceback.format_exc()[-800:]}


@router.get("/trajectories/image")
def image():
    """The most recently rendered trajectory plot."""
    p = _LAST_RENDER.get('path')
    if not p or not os.path.exists(p):
        return {'error': 'Nothing rendered yet'}
    # Read bytes and return directly: FileResponse streaming can hit
    # ERR_CONTENT_LENGTH_MISMATCH on Windows (file locks / size races).
    from fastapi.responses import Response
    with open(p, 'rb') as f:
        data = f.read()
    return Response(content=data, media_type='image/png',
                    headers={'Cache-Control': 'no-store',
                             'Content-Disposition':
                             f'inline; filename="{os.path.basename(p)}"'})
