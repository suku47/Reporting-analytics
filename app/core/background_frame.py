"""Clean background frames for trajectory plots.

Finds a frame with zero detections (empty road) using the .traf's own
observations table, grabs it from the video ONCE, and stores it inside
the .traf (assets table). From then on, trajectory plots render without
needing the video at all — the .traf is self-contained.
"""

import logging
import sqlite3

import cv2
import numpy as np

log = logging.getLogger(__name__)

ASSETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    key  TEXT PRIMARY KEY,
    data BLOB
)"""


def find_clean_frame(conn, total_frames=None):
    """Pick the frame most likely to show an empty road: the middle of the
    longest run of frames with zero observations. Falls back to the frame
    with the fewest observations if the road is never empty."""
    if total_frames is None:
        try:
            meta = dict(conn.execute("SELECT key, value FROM scene"))
            total_frames = int(meta.get('total_frames', 0))
        except Exception:
            total_frames = 0

    frames = [r[0] for r in conn.execute(
        "SELECT DISTINCT frame FROM observations ORDER BY frame")]
    if not frames:
        return 0  # no detections anywhere — any frame is clean

    # Gaps: before first, between consecutive, after last
    best_len, best_mid = -1, 0
    prev = -1
    endpoints = frames + ([total_frames - 1] if total_frames else [])
    for f in endpoints:
        gap = f - prev - 1
        if gap > best_len:
            best_len, best_mid = gap, prev + 1 + gap // 2
        prev = f

    if best_len >= 5:
        log.info(f"Clean background: frame {best_mid} "
                 f"(middle of a {best_len}-frame empty gap)")
        return best_mid

    # Never empty: least-busy frame
    row = conn.execute(
        "SELECT frame, COUNT(*) c FROM observations "
        "GROUP BY frame ORDER BY c ASC, frame ASC LIMIT 1").fetchone()
    log.info(f"No empty gap found — using least-busy frame {row[0]} "
             f"({row[1]} vehicles visible)")
    return row[0]


def capture_and_store_background(traf_path, video_path):
    """Grab the cleanest frame from the video and store it in the .traf.
    Idempotent: skips if already stored. Returns the frame index used,
    or None on failure."""
    conn = sqlite3.connect(traf_path)
    try:
        conn.execute(ASSETS_SCHEMA)
        if conn.execute("SELECT 1 FROM assets WHERE key='background_frame'"
                        ).fetchone():
            return int(dict(conn.execute(
                "SELECT key, value FROM scene")).get('background_frame_idx', 0))

        idx = find_clean_frame(conn)
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        use_idx = min(idx, max(0, n - 1)) if n else idx
        cap.set(cv2.CAP_PROP_POS_FRAMES, use_idx)
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
            use_idx = 0
        cap.release()
        if not ok:
            return None

        ok, png = cv2.imencode('.png', frame)
        if not ok:
            return None
        conn.execute("INSERT OR REPLACE INTO assets VALUES ('background_frame', ?)",
                     (png.tobytes(),))
        conn.execute("INSERT OR REPLACE INTO scene VALUES "
                     "('background_frame_idx', ?)", (str(use_idx),))
        conn.commit()
        log.info(f"Stored clean background (frame {use_idx}) inside "
                 f"{traf_path}")
        return use_idx
    finally:
        conn.close()


def load_stored_background(traf_path_or_conn):
    """Return the stored background as a BGR ndarray, or None."""
    own = isinstance(traf_path_or_conn, str)
    conn = sqlite3.connect(traf_path_or_conn) if own else traf_path_or_conn
    try:
        try:
            row = conn.execute(
                "SELECT data FROM assets WHERE key='background_frame'"
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # no assets table
        if not row:
            return None
        buf = np.frombuffer(row[0], dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    finally:
        if own:
            conn.close()


def resolve_background(traf_path, video_path=None, frame='auto'):
    """The one entry point: returns (canvas, description).
    Priority: explicit video (auto-storing the clean frame for the future)
    → stored asset in the .traf → error."""
    if video_path:
        import os
        if os.path.exists(video_path):
            if frame == 'auto':
                idx = capture_and_store_background(traf_path, video_path)
                img = load_stored_background(traf_path)
                if img is not None:
                    return img, f"clean frame {idx} (stored in .traf)"
                frame = 0
            cap = cv2.VideoCapture(video_path)
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame))
            ok, img = cap.read()
            cap.release()
            if ok:
                return img, f"video frame {frame}"

    img = load_stored_background(traf_path)
    if img is not None:
        return img, "clean frame stored in .traf"

    raise FileNotFoundError(
        "No background available: no video provided and this .traf has no "
        "stored background frame. Open it once with the video (or set "
        "background_video in the batch config) and the clean frame will be "
        "captured and stored automatically.")
