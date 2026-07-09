"""
Shared database state and query helpers.
All API modules import from here instead of server.py.
"""

import os
import sqlite3

# Global state
state = {
    'db_path': None,
    'video_path': None,
    'conn': None,
    'fps': 30.0,
    'frame_width': 1280,
    'frame_height': 720,
    'total_frames': 0,
}


def get_conn() -> sqlite3.Connection:
    """Get the active database connection."""
    if state['conn'] is None:
        raise RuntimeError("No .traf file loaded")
    return state['conn']


def query(sql, params=(), one=False):
    """Execute query and return list of dicts (or single dict if one=True)."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    if one:
        return rows[0] if rows else None
    return rows


def load_traf(traf_path, license_key=None):
    """Load a .traf or .etraf file into the global state."""
    if traf_path.endswith('.etraf'):
        if not license_key:
            raise ValueError("License key required for .etraf files")
        from app.core.traf_security import decrypt_traf
        traf_path = decrypt_traf(traf_path, license_key)

    if not os.path.exists(traf_path):
        raise FileNotFoundError(f"File not found: {traf_path}")

    conn = sqlite3.connect(traf_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    state['conn'] = conn
    state['db_path'] = traf_path

    # Load metadata
    meta = {r['key']: r['value']
            for r in conn.execute("SELECT key, value FROM scene")}
    state['fps'] = float(meta.get('fps', 30.0))
    state['frame_width'] = int(meta.get('frame_width', 1280))
    state['frame_height'] = int(meta.get('frame_height', 720))
    state['total_frames'] = int(meta.get('total_frames', 0))

    track_count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    print(f"  Loaded: {traf_path}")
    print(f"  Tracks: {track_count}  |  FPS: {state['fps']}  |  "
          f"Frame: {state['frame_width']}x{state['frame_height']}")


def load_video(video_path):
    """Set video path for frame extraction."""
    if os.path.exists(video_path):
        state['video_path'] = os.path.abspath(video_path)
        print(f"  Video:  {video_path}")
    else:
        print(f"  Warning: Video not found: {video_path}")


def load_image(image_path):
    """Load a static background image (alternative to video)."""
    if not os.path.exists(image_path):
        print(f"  Warning: Image not found: {image_path}")
        return
    try:
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            print(f"  Warning: Could not read image: {image_path}")
            return
        _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 90])

        # Store in video.py's static image bytes
        from app.api import video as video_api
        video_api._static_image_bytes = buf.tobytes()

        state['frame_width'] = img.shape[1]
        state['frame_height'] = img.shape[0]
        state['video_path'] = None  # no video, image mode
        print(f"  Image:  {image_path} ({img.shape[1]}x{img.shape[0]})")
    except ImportError:
        print("  Warning: opencv-python required for image loading")
