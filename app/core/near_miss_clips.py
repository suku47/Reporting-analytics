"""Near-miss clip renderer.

Reproduces the old near_miss_analyzer.py output in the viewer: for one
conflict, cut the source video around the event and burn in trajectory
trails + bounding boxes for ONLY the two involved tracks, plus a
severity/PET OSD strip and a pulsing conflict-point marker.

Clips are written to <traf_dir>/nearmiss_clips/ and cached — rendering
the same conflict twice reuses the file. Encoding priority:
  1. imageio-ffmpeg bundled ffmpeg → libx264 yuv420p (plays in any browser)
  2. cv2 'avc1' (works when an OpenH264 dll is present)
  3. cv2 'mp4v' (always writes, but browsers can't play it — the frontend
     falls back to raw-stream playback and the file stays on disk)
"""

import json
import logging
import os
import sqlite3
import subprocess

import cv2
import numpy as np

log = logging.getLogger(__name__)

PAD_SEC = 4.0                       # context before/after the conflict frame
BOX_HOLD_FRAMES = 5                 # keep last bbox this long across obs gaps
COL_A = (0, 165, 255)               # BGR orange  — track A
COL_B = (255, 200, 0)               # BGR cyan    — track B
SEV_COLOR = {'critical': (115, 93, 255), 'severe': (60, 140, 255),
             'moderate': (61, 197, 255), 'slight': (189, 167, 154)}


# ── data loading ──────────────────────────────────────────────────────────

def _load_track(conn, tid):
    """Per-frame observations for one track: sorted arrays + bbox lookup."""
    rows = conn.execute(
        "SELECT frame, cx, cy, bbox_x1, bbox_y1, bbox_x2, bbox_y2 "
        "FROM observations WHERE track_id=? ORDER BY frame", (tid,)).fetchall()
    if not rows:
        # Fallback: sampled trajectory_json [[x,y,frame],...] (no bboxes)
        r = conn.execute("SELECT trajectory_json FROM tracks WHERE track_id=?",
                         (tid,)).fetchone()
        pts = json.loads(r[0]) if r and r[0] else []
        rows = [(int(p[2]), float(p[0]), float(p[1]),
                 None, None, None, None) for p in pts]
    frames = np.array([r[0] for r in rows], dtype=np.int64)
    cxy = np.array([[r[1], r[2]] for r in rows], dtype=np.float32)
    boxes = {r[0]: (r[3], r[4], r[5], r[6]) for r in rows
             if r[3] is not None}
    return {'frames': frames, 'cxy': cxy, 'boxes': boxes}


def _cls_name(conn, tid):
    r = conn.execute("SELECT class_name FROM tracks WHERE track_id=?",
                     (tid,)).fetchone()
    return r[0] if r else '?'


# ── drawing ───────────────────────────────────────────────────────────────

def _draw_trail(img, trk, upto_frame, color):
    """Fading polyline of the track's path up to the current frame."""
    idx = np.searchsorted(trk['frames'], upto_frame, side='right')
    pts = trk['cxy'][:idx]
    if len(pts) < 2:
        return
    n = len(pts)
    step = max(1, n // 400)          # cap segment count on long tracks
    for i in range(step, n, step):
        a = tuple(np.round(pts[i - step]).astype(int))
        b = tuple(np.round(pts[i]).astype(int))
        alpha = i / n                # older = fainter
        col = tuple(int(c * (0.35 + 0.65 * alpha)) for c in color)
        cv2.line(img, a, b, col, 2, cv2.LINE_AA)


def _box_at(trk, frame):
    """bbox at frame, holding the last seen box across small gaps."""
    for f in range(frame, frame - BOX_HOLD_FRAMES - 1, -1):
        if f in trk['boxes']:
            return trk['boxes'][f]
    return None


def _draw_box(img, trk, frame, color, label):
    box = _box_at(trk, frame)
    if box is None:
        return
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
    ty = y1 - 6 if y1 - th - 10 > 0 else y2 + th + 6
    cv2.rectangle(img, (x1, ty - th - 4), (x1 + tw + 8, ty + 4), color, -1)
    cv2.putText(img, label, (x1 + 4, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (10, 10, 10), 2, cv2.LINE_AA)


def _draw_conflict_marker(img, point, frame, event_frame, fps, sev_color):
    px, py = int(round(point[0])), int(round(point[1]))
    # Always-visible marker: white halo + filled severity dot + large cross
    cv2.circle(img, (px, py), 10, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.circle(img, (px, py), 6, sev_color, -1, cv2.LINE_AA)
    cv2.drawMarker(img, (px, py), (255, 255, 255), cv2.MARKER_CROSS, 34, 4)
    cv2.drawMarker(img, (px, py), sev_color, cv2.MARKER_CROSS, 30, 2)
    # Pulsing ring for ±1.5s around the event moment
    if abs(frame - event_frame) <= fps * 1.5:
        phase = (frame % max(int(fps / 3), 1)) / max(int(fps / 3), 1)
        r = int(18 + 16 * phase)
        cv2.circle(img, (px, py), r, sev_color, 3, cv2.LINE_AA)
        cv2.circle(img, (px, py), r + 3, (255, 255, 255), 1, cv2.LINE_AA)


def _draw_clock(img, frame, fps, start_dt, w, h):
    """Clean re-drawn clock (replaces the scrubbed camera timestamp).
    Real clock time when the traf carries video_start_time, else elapsed."""
    if start_dt is not None:
        from datetime import timedelta
        t = start_dt + timedelta(seconds=frame / fps)
        txt = t.strftime('%Y-%m-%d %H:%M:%S')
    else:
        secs = frame / fps
        txt = f'T+{int(secs // 60):02d}:{secs % 60:04.1f}'
    org = (14, 30)
    cv2.putText(img, txt, org, cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                (10, 10, 10), 5, cv2.LINE_AA)
    cv2.putText(img, txt, org, cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                (240, 240, 240), 2, cv2.LINE_AA)


def _draw_osd(img, event, frame, fps, w, h):
    metric = (f"PET {event['pet']}s" if event.get('pet') is not None
              else f"TTC {event['ttc']}s")
    txt = (f"NEAR MISS  |  {event['severity'].upper()}  |  {metric}  |  "
           f"{event['a_cls']} #{event['a_id']} x {event['b_cls']} #{event['b_id']}"
           f"  |  t={frame / fps:.1f}s  f={frame}")
    bar_h = 34
    cv2.rectangle(img, (0, h - bar_h), (w, h), (18, 14, 10), -1)
    cv2.putText(img, txt, (12, h - 11), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                SEV_COLOR.get(event['severity'], (200, 200, 200)), 1,
                cv2.LINE_AA)


# ── encoding ──────────────────────────────────────────────────────────────

class _FFmpegWriter:
    def __init__(self, exe, w, h, fps, out_path):
        cmd = [exe, '-y', '-loglevel', 'error',
               '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{w}x{h}',
               '-r', f'{fps:.6f}', '-i', '-',
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
               '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out_path]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, frame):
        self.proc.stdin.write(frame.tobytes())

    def release(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=120)


def _open_writer(w, h, fps, out_path):
    """Returns (codec_tag, writer). codec_tag 'h264' = browser-playable."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return 'h264', _FFmpegWriter(exe, w, h, fps, out_path)
    except Exception as e:
        log.info(f"imageio-ffmpeg unavailable ({e}); trying cv2 writers")
    vw = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*'avc1'),
                         fps, (w, h))
    if vw.isOpened():
        return 'h264', vw
    vw = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*'mp4v'),
                         fps, (w, h))
    if not vw.isOpened():
        raise RuntimeError("no working video writer (install imageio-ffmpeg)")
    return 'mp4v', vw


# ── public API ────────────────────────────────────────────────────────────

def clips_dir(traf_path):
    d = os.path.join(os.path.dirname(os.path.abspath(traf_path)),
                     'nearmiss_clips')
    os.makedirs(d, exist_ok=True)
    return d


def clip_name(traf_path, event):
    stem = os.path.splitext(os.path.basename(traf_path))[0]
    return (f"{stem}_{event['severity']}_A{event['a_id']}_B{event['b_id']}"
            f"_f{event['frame']}.mp4")


def render_clip(traf_path, video_path, event, fps, pad_sec=PAD_SEC):
    """Render (or reuse) the annotated clip for one event.
    Returns (out_path, codec_tag, cached)."""
    out_path = os.path.join(clips_dir(traf_path), clip_name(traf_path, event))
    if os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
        return out_path, 'h264', True     # cached: assume prior good encode

    if not video_path or not os.path.exists(video_path):
        raise FileNotFoundError("No video loaded — start the viewer with --video")

    conn = sqlite3.connect(traf_path)
    try:
        ta = _load_track(conn, event['a_id'])
        tb = _load_track(conn, event['b_id'])
        start_dt = None
        try:
            from datetime import datetime
            row = conn.execute("SELECT value FROM scene "
                               "WHERE key='video_start_time'").fetchone()
            if row and row[0]:
                start_dt = datetime.fromisoformat(row[0])
        except Exception:
            start_dt = None
        lbl_a = f"{event.get('a_cls') or _cls_name(conn, event['a_id'])} #{event['a_id']}"
        lbl_b = f"{event.get('b_cls') or _cls_name(conn, event['b_id'])} #{event['b_id']}"
    finally:
        conn.close()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        # even dimensions required for yuv420p
        we, he = w - (w % 2), h - (h % 2)

        ef = int(event['frame'])
        f0 = max(0, ef - int(pad_sec * fps))
        f1 = min(total - 1 if total > 0 else ef + int(pad_sec * fps),
                 ef + int(pad_sec * fps))

        codec, writer = _open_writer(we, he, fps, out_path)
        sev_col = SEV_COLOR.get(event['severity'], (200, 200, 200))
        point = event.get('point') or [w / 2, h / 2]

        # OSD scrub: watermark position is static — detect once, reuse
        osd = None
        try:
            from app.core.osd_scrub import osd_mask, scrub_osd
            _scrub = scrub_osd
        except Exception:
            _scrub = None

        cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
        for f in range(f0, f1 + 1):
            ret, frame = cap.read()
            if not ret:
                break
            if _scrub is not None:
                if osd is None:
                    osd = osd_mask(frame)
                    if osd is None:
                        _scrub = None      # no OSD in this footage
                if _scrub is not None:
                    frame = _scrub(frame, osd)
            _draw_clock(frame, f, fps, start_dt, w, h)
            _draw_trail(frame, ta, f, COL_A)
            _draw_trail(frame, tb, f, COL_B)
            _draw_box(frame, ta, f, COL_A, lbl_a)
            _draw_box(frame, tb, f, COL_B, lbl_b)
            _draw_conflict_marker(frame, point, f, ef, fps, sev_col)
            _draw_osd(frame, event, f, fps, w, h)
            if (we, he) != (w, h):
                frame = frame[:he, :we]
            writer.write(frame)
        writer.release()
    finally:
        cap.release()

    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1_000:
        raise RuntimeError("clip encoding produced no output")
    return out_path, codec, False
