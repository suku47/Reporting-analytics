import io
import os
import threading
from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from fastapi.responses import StreamingResponse, Response
from app.core.database import state

router = APIRouter(tags=["video"])

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

# Persistent video capture (avoid open/close per frame)
_video_cap = None
_video_lock = threading.Lock()
_frame_cache = {}
_CACHE_MAX = 100

# Static image mode
_static_image_bytes = None


def _get_video_cap():
    """Get or create persistent VideoCapture."""
    global _video_cap
    if _video_cap is None or not _video_cap.isOpened():
        if not state['video_path']:
            return None
        _video_cap = cv2.VideoCapture(state['video_path'])
        if not _video_cap.isOpened():
            _video_cap = None
            return None
    return _video_cap


_STREAM_CHUNK = 4 * 1024 * 1024   # max bytes per range response


@router.get("/video/stream")
def video_stream(request: Request):
    """Serve the loaded video with HTTP Range support so the browser's
    native <video> element can seek instantly (near-miss clip playback).
    Bytes-based Response with explicit Content-Length — FileResponse
    breaks on Windows (ERR_CONTENT_LENGTH_MISMATCH)."""
    path = state.get('video_path')
    if not path or not os.path.exists(path):
        raise HTTPException(404, "No video loaded. Pass --video when starting.")
    size = os.path.getsize(path)

    start, end = 0, size - 1
    rng = request.headers.get('range')
    if rng and rng.lower().startswith('bytes='):
        parts = rng.split('=', 1)[1].split('-')
        if parts[0].strip():
            start = max(0, int(parts[0]))
        if len(parts) > 1 and parts[1].strip():
            end = min(int(parts[1]), size - 1)
    if start >= size:
        raise HTTPException(416, "Range not satisfiable")
    end = min(end, start + _STREAM_CHUNK - 1)

    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read(end - start + 1)

    ext = os.path.splitext(path)[1].lower()
    media = {'.mp4': 'video/mp4', '.m4v': 'video/mp4',
             '.mov': 'video/quicktime', '.mkv': 'video/x-matroska',
             '.avi': 'video/x-msvideo'}.get(ext, 'video/mp4')
    return Response(content=data, status_code=206, media_type=media,
                    headers={'Content-Range': f'bytes {start}-{end}/{size}',
                             'Accept-Ranges': 'bytes',
                             'Content-Length': str(len(data)),
                             'Cache-Control': 'no-store'})


@router.get("/reference_frame")
def get_reference_frame():
    """First frame or static image."""
    if _static_image_bytes:
        return Response(content=_static_image_bytes, media_type='image/jpeg')
    return _extract_frame(0)


@router.get("/frame_image/{frame_num}")
def get_frame_image(frame_num: int):
    """Extract a specific frame. Priority: uploaded/–-image static image →
    video frame → clean background stored inside the .traf (self-contained
    mode: no video or image needed at all)."""
    global _static_image_bytes
    if _static_image_bytes:
        return Response(content=_static_image_bytes, media_type='image/jpeg')
    # Priority: explicit --image (above) → stored CLEAN frame → video.
    # The clean zero-detection frame beats video frame 0, which is often
    # an occluded setup shot (operator in front of the lens).
    try:
        from app.core.background_frame import load_stored_background
        img = load_stored_background(state.get('db_path'))
        if img is not None:
            _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 90])
            _static_image_bytes = buf.tobytes()   # cache for next calls
            return Response(content=_static_image_bytes, media_type='image/jpeg')
    except Exception:
        pass
    return _extract_frame(frame_num)


@router.post("/upload_image")
async def upload_background_image(file: UploadFile = File(...)):
    """Upload a static background image (instead of video)."""
    global _static_image_bytes
    if not HAS_CV2:
        raise HTTPException(500, "opencv-python not installed")

    contents = await file.read()
    import numpy as np
    arr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Invalid image file")

    # Encode as JPEG
    _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    _static_image_bytes = buf.tobytes()

    state['frame_width'] = img.shape[1]
    state['frame_height'] = img.shape[0]

    return {'status': 'ok', 'width': img.shape[1], 'height': img.shape[0]}


def _extract_frame(frame_num: int):
    if not HAS_CV2:
        raise HTTPException(500, "opencv-python not installed")
    if not state['video_path']:
        raise HTTPException(404, "No video loaded. Pass --video when starting.")

    # Check cache
    if frame_num in _frame_cache:
        return Response(content=_frame_cache[frame_num], media_type='image/jpeg')

    with _video_lock:
        cap = _get_video_cap()
        if cap is None:
            raise HTTPException(404, "Could not open video file")

        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = cap.read()

    if not ret:
        raise HTTPException(404, f"Frame {frame_num} not available")

    try:                                  # remove camera OSD watermark
        from app.core.osd_scrub import scrub_osd
        frame = scrub_osd(frame)
    except Exception:
        pass
    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    jpeg_bytes = buf.tobytes()

    # Cache (LRU-like: evict oldest when full)
    if len(_frame_cache) >= _CACHE_MAX:
        oldest = next(iter(_frame_cache))
        del _frame_cache[oldest]
    _frame_cache[frame_num] = jpeg_bytes

    return Response(content=jpeg_bytes, media_type='image/jpeg')
