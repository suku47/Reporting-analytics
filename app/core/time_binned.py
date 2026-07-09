"""
Time-Binned Traffic Counting Engine

Computes directional vehicle counts (gate A → gate B) broken down by:
  - User-defined time bins (1, 5, 15, 30, 60 minutes)
  - Vehicle classification (PV, SU, CU, etc. — excludes Peds)
  - Optional time-range filter (e.g. 08:00–10:00)

Time derivation:
  real_time = video_start_time + (frame / fps)
  If video_start_time is NULL, falls back to elapsed seconds from frame 0.
"""

import sqlite3
import json
from datetime import datetime, timedelta
from collections import defaultdict


# Classes to always exclude from traffic counts
def _get_excluded_classes(conn):
    try:
        from app.core.class_profile import get_class_profile
        profile = get_class_profile(conn)
        return profile['excluded_classes']
    except Exception:
        return {'Peds', 'Pedestrian', 'Ped', 'Cyclist', 'Bicycle'}

def _get_vehicle_classes(conn):
    try:
        from app.core.class_profile import get_class_profile
        profile = get_class_profile(conn)
        return profile['vehicle_classes']
    except Exception:
        return ['PV', 'SU', 'CU']


def _get_video_time_info(conn):
    """Extract video_start_time, fps, total_frames from scene metadata."""
    meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM scene")}
    fps = float(meta.get('fps', 30.0))
    total_frames = int(meta.get('total_frames', 0))

    start_time = None
    raw = meta.get('video_start_time')
    if raw and raw != 'None' and raw != 'null':
        try:
            start_time = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            pass

    return start_time, fps, total_frames


def get_time_range_info(conn):
    """
    Return the video's time range for the UI (so users can pick start/end).
    Returns dict with start_time, end_time, duration_sec, has_real_time.
    """
    start_time, fps, total_frames = _get_video_time_info(conn)
    duration_sec = total_frames / fps if fps > 0 else 0

    if start_time:
        end_time = start_time + timedelta(seconds=duration_sec)
        return {
            'has_real_time': True,
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'start_display': start_time.strftime('%H:%M:%S'),
            'end_display': end_time.strftime('%H:%M:%S'),
            'duration_sec': round(duration_sec, 1),
            'fps': fps,
            'total_frames': total_frames,
        }
    else:
        return {
            'has_real_time': False,
            'start_time': None,
            'end_time': None,
            'start_display': '00:00:00',
            'end_display': str(timedelta(seconds=int(duration_sec))),
            'duration_sec': round(duration_sec, 1),
            'fps': fps,
            'total_frames': total_frames,
        }


def _frame_to_datetime(frame, start_time, fps):
    """Convert frame number to datetime (or elapsed timedelta if no start_time)."""
    seconds = frame / fps if fps > 0 else 0
    if start_time:
        return start_time + timedelta(seconds=seconds)
    return timedelta(seconds=seconds)


def _compute_time_bins(start_dt, end_dt, bin_minutes):
    bins = []
    delta = timedelta(minutes=bin_minutes)
    # Snap start DOWN to nearest clock boundary
    minute = start_dt.minute
    snapped_minute = (minute // bin_minutes) * bin_minutes
    bin_start = start_dt.replace(minute=snapped_minute, second=0, microsecond=0)
    # Snap end UP to nearest clock boundary
    end_total_min = end_dt.hour * 60 + end_dt.minute
    if end_dt.second > 0 or end_dt.microsecond > 0:
        end_total_min += 1
    snapped_end_min = ((end_total_min + bin_minutes - 1) // bin_minutes) * bin_minutes
    hours_add = snapped_end_min // 60
    mins_add = snapped_end_min % 60
    bin_end_snap = end_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=hours_add, minutes=mins_add)
    current = bin_start
    while current < bin_end_snap:
        next_edge = current + delta
        label = current.strftime("%H:%M") + " - " + next_edge.strftime("%H:%M")
        bins.append((current, next_edge, label))
        current = next_edge
    return bins


def _compute_elapsed_bins(duration_sec, bin_minutes):
    """Generate bins for elapsed time (no real clock)."""
    bins = []
    bin_sec = bin_minutes * 60
    current = 0

    while current < duration_sec:
        bin_end = min(current + bin_sec, duration_sec)
        label_start = str(timedelta(seconds=int(current)))
        label_end = str(timedelta(seconds=int(bin_end)))
        bins.append((current, bin_end, f"{label_start} - {label_end}"))
        current = bin_end

    return bins


def compute_time_binned_movements(conn, movements, bin_minutes=15,
                                   range_start=None, range_end=None):
    """
    Core computation: for each movement (gate_from → gate_to), count crossings
    per time bin per vehicle class.

    Args:
        conn: SQLite connection to .traf database
        movements: list of dicts [{from_id, to_id, from_name, to_name}, ...]
        bin_minutes: bin size in minutes (1, 5, 15, 30, 60)
        range_start: ISO time string or HH:MM to filter start (optional)
        range_end: ISO time string or HH:MM to filter end (optional)

    Returns:
        dict with:
          - time_bins: list of bin labels
          - movements: list of movement results, each with per-bin per-class counts
          - class_names: ordered list of vehicle classes found
          - meta: time range info
    """
    start_time, fps, total_frames = _get_video_time_info(conn)
    has_real_time = start_time is not None
    duration_sec = total_frames / fps if fps > 0 else 0

    # Parse user time range
    filter_start_dt = None
    filter_end_dt = None

    if has_real_time:
        video_start = start_time
        video_end = start_time + timedelta(seconds=duration_sec)

        if range_start:
            filter_start_dt = _parse_time_input(range_start, start_time)
        if range_end:
            filter_end_dt = _parse_time_input(range_end, start_time)

        period_start = filter_start_dt or video_start
        period_end = filter_end_dt or video_end

        time_bins = _compute_time_bins(period_start, period_end, bin_minutes)
    else:
        filter_start_sec = 0
        filter_end_sec = duration_sec
        if range_start:
            try:
                filter_start_sec = _parse_elapsed(range_start)
            except Exception:
                pass
        if range_end:
            try:
                filter_end_sec = _parse_elapsed(range_end)
            except Exception:
                pass

        period_start = filter_start_sec
        period_end = filter_end_sec
        time_bins = _compute_elapsed_bins(period_end - period_start, bin_minutes)

    excluded = _get_excluded_classes(conn)

    # Get all vehicle classes (excluding non-vehicles)
    all_classes = conn.execute(
        "SELECT DISTINCT class_name FROM gate_crossings "
        "WHERE class_name IS NOT NULL ORDER BY class_name"
    ).fetchall()
    class_names = [r[0] for r in all_classes if r[0] not in excluded]
    if not class_names:
        class_names = _get_vehicle_classes(conn)

    # Direct + inferred assignments (single pass for all movements).
    # Inferred = single-crossing tracks recovered via learned per-site
    # direction-sign / heading signatures. Display-only; .traf untouched.
    from app.core.movement_inference import get_movement_assignments
    assign_data = get_movement_assignments(conn, movements)

    # For each movement, bin its assigned tracks
    movement_results = []

    for mov in movements:
        from_id = mov['from_id']
        to_id = mov['to_id']
        from_name = mov.get('from_name', str(from_id))
        to_name = mov.get('to_name', str(to_id))

        rows = assign_data['assignments'].get((from_id, to_id), [])

        # Bin each assigned track by its entry-crossing frame
        # bins_data[bin_index][class_name] = count (direct + inferred)
        bins_data = [{cls: 0 for cls in class_names} for _ in time_bins]
        total_per_class = {cls: 0 for cls in class_names}
        direct_per_class = {cls: 0 for cls in class_names}
        inferred_per_class = {cls: 0 for cls in class_names}
        grand_total = 0

        for a in rows:
            frame = a['entry_frame']
            cls = a['class_name']
            if cls in excluded:
                continue
            if cls not in class_names:
                continue

            if has_real_time:
                event_time = _frame_to_datetime(frame, start_time, fps)
                bin_idx = _find_bin_index_dt(event_time, time_bins)
            else:
                event_sec = frame / fps if fps > 0 else 0
                event_sec_offset = event_sec - period_start
                bin_idx = _find_bin_index_elapsed(event_sec_offset, bin_minutes * 60, len(time_bins))

            if bin_idx is not None and 0 <= bin_idx < len(time_bins):
                bins_data[bin_idx][cls] += 1
                total_per_class[cls] += 1
                if a['inferred']:
                    inferred_per_class[cls] += 1
                else:
                    direct_per_class[cls] += 1
                grand_total += 1

        movement_results.append({
            'from_id': from_id,
            'to_id': to_id,
            'from_name': from_name,
            'to_name': to_name,
            'label': f"{from_name} → {to_name}" + (' (U-turn)' if from_id == to_id else ''),
            'bins': bins_data,
            'total_per_class': total_per_class,
            'direct_per_class': direct_per_class,
            'inferred_per_class': inferred_per_class,
            'direct_total': sum(direct_per_class.values()),
            'inferred_total': sum(inferred_per_class.values()),
            'grand_total': grand_total,
        })

    # Build the grand total across all movements per bin
    grand_bins = [{cls: 0 for cls in class_names} for _ in time_bins]
    for mov in movement_results:
        for bi, bin_counts in enumerate(mov['bins']):
            for cls in class_names:
                grand_bins[bi][cls] += bin_counts.get(cls, 0)

    return {
        'time_bins': [b[2] for b in time_bins],
        'bin_minutes': bin_minutes,
        'movements': movement_results,
        'class_names': class_names,
        'grand_totals_per_bin': grand_bins,
        'has_real_time': has_real_time,
        'period_start': period_start.isoformat() if has_real_time and isinstance(period_start, datetime) else str(period_start),
        'period_end': period_end.isoformat() if has_real_time and isinstance(period_end, datetime) else str(period_end),
        'inference': {
            'assignments': assign_data['assignments'],
            'unresolved': assign_data['unresolved'],
            'report': assign_data['report'],
        },
    }


def _parse_time_input(time_str, reference_date):
    """
    Parse a time input that could be:
      - "08:00" or "8:00" → use reference_date's date + this time
      - "08:00:00" → same with seconds
      - ISO datetime string → parse directly
    """
    time_str = time_str.strip()

    # Try ISO datetime first
    try:
        return datetime.fromisoformat(time_str)
    except (ValueError, TypeError):
        pass

    # Try HH:MM or HH:MM:SS
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            t = datetime.strptime(time_str, fmt).time()
            return datetime.combine(reference_date.date(), t)
        except (ValueError, TypeError):
            pass

    return None


def _parse_elapsed(val):
    """Parse elapsed time input: could be seconds (float) or 'HH:MM:SS' string."""
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(val)
    except (ValueError, TypeError):
        pass
    # Try HH:MM:SS
    parts = str(val).split(':')
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 3600 + int(parts[1]) * 60
    return 0


def _find_bin_index_dt(event_time, time_bins):
    """Find which bin a datetime event falls into."""
    for i, (bin_start, bin_end, _) in enumerate(time_bins):
        if bin_start <= event_time < bin_end:
            return i
    # Edge case: exactly at end
    if time_bins and event_time == time_bins[-1][1]:
        return len(time_bins) - 1
    return None


def _find_bin_index_elapsed(offset_sec, bin_sec, num_bins):
    """Find bin index for elapsed time offset."""
    if offset_sec < 0:
        return None
    idx = int(offset_sec // bin_sec)
    if idx >= num_bins:
        return num_bins - 1
    return idx
