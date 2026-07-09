from fastapi import APIRouter
from app.core.database import get_conn, state
from app.core.analytics import compute_od_matrix, compute_speed_analysis, detect_near_misses

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/od_matrix")
def od_matrix():
    """Origin-Destination matrix."""
    return compute_od_matrix(get_conn())


@router.get("/speed")
def speed_analysis():
    """Speed breakdown by class, edge, and time."""
    return compute_speed_analysis(get_conn(), fps=state['fps'])


@router.get("/near_misses")
def near_misses(min_distance: float = 50.0, ttc_threshold: float = 2.0):
    """Detect near-miss events."""
    return detect_near_misses(
        get_conn(), fps=state['fps'],
        min_distance_px=min_distance,
        ttc_threshold_sec=ttc_threshold)
