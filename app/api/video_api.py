import io
import os
import threading
from fastapi import APIRouter, HTTPException, UploadFile, File
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
    if state.get('video_path'):
        return _extract_frame(frame_num)
    # Self-contained fallback: the traf's own stored clean frame
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

    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    jpeg_bytes = buf.tobytes()

    # Cache (LRU-like: evict oldest when full)
    if len(_frame_cache) >= _CACHE_MAX:
        oldest = next(iter(_frame_cache))
        del _frame_cache[oldest]
    _frame_cache[frame_num] = jpeg_bytes

    return Response(content=jpeg_bytes, media_type='image/jpeg')
