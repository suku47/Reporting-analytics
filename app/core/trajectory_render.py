"""
trajectory_plot.py — Draw all vehicle and pedestrian trajectories from a
                     .traf file onto a single background frame.

Static-image output (one PNG/JPG with all trails drawn cumulatively),
in the same style as ped_visualizer.py — but supports any class, with
each vehicle class drawn in its own color and pedestrians drawn in red.

Usage:
  python trajectory_plot.py \
      --traf path/to/output.traf \
      --video path/to/source_video.mp4 \
      --out path/to/trajectories.png

Optional:
  --frame N             Use frame N as background (default: 0 = first frame)
  --thickness N         Polyline thickness in px (default: 1)
  --min-points N        Skip tracks with fewer than N points (default: 10)
  --per-class N         Keep at most N trajectories per class — preserves
                        pattern visibility on busy scenes.
                        Example: --per-class 50
  --classes A,B,C       Only draw these class short names (default: all)
                        e.g. --classes Car,Bike,Truck,Bus,Auto
                        e.g. --classes Ped              (peds only — same as ped_visualizer.py)
  --skip-stationary     Skip tracks flagged is_stationary=1 in the .traf
  --legend              Draw a small color legend in the top-left corner
  --seed N              Random seed for --per-class sampling (default: 42)
  --jpg                 Save as JPG (quality 95) instead of PNG
"""

import argparse
import json
import logging
import os
import sqlite3
import sys

import cv2
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("trajectory_plot")


# ─────────────────────────────────────────────────────────────────────────
# Class colors — matches track_visualizer.py palette (BGR)
# ─────────────────────────────────────────────────────────────────────────

# Colors are stored as OpenCV BGR tuples (Blue, Green, Red).
# The trailing comment names the on-screen (RGB) appearance of each value.
CLASS_COLORS = {
    # ── India profile ────────────────────────────────────────────────
    'Car':     (255, 255, 0),    # Cyan          BGR(255,255,  0)
    'Bike':    (255, 140, 188),  # Light blue    BGR(255,140,188)
    'Truck':   (0, 215, 255),    # Gold/Yellow   BGR(  0,215,255)
    'Bus':     (0, 140, 255),    # Orange        BGR(  0,140,255)
    'Auto':    (0, 255, 128),    # Spring green  BGR(  0,255,128)
    'Cyclist': (200, 200, 0),    # Dark cyan     BGR(200,200,  0)
    'Ped':     (0, 0, 255),      # Red           BGR(  0,  0,255)  — pedestrians, matches ped_visualizer.py
    # ── US profile — colors sampled from the DataFromSky-style reference
    #    screenshot legend (kept so this script works for older .traf too) ──
    'PV':      (220, 161, 101),  # Sky blue      BGR(220,161,101)
    'SU':      (95, 170, 85),    # Green         BGR( 95,170, 85)
    'CU':      (69, 152, 197),   # Amber         BGR( 69,152,197)
    'BUS':     (67, 114, 192),   # Orange        BGR( 67,114,192)
    'MC':      (232, 146, 181),  # Purple        BGR(232,146,181)
    'PVT':     (177, 128, 227),  # Pink          BGR(177,128,227)
    'Bicycle': (235, 187, 133),  # Light blue    BGR(235,187,133)
    # NOTE: 'Ped' stays red (defined in India section above) to match
    # ped_visualizer.py — the reference screenshot shows it light orange
    # BGR(90,140,219), switch it there if you want that instead.
    # ── UK profile ───────────────────────────────────────────────────
    # 'Car' (cyan) is defined in the India section above — UK uses the same.
    'LGV':     (0, 200, 100),    # Green         BGR(  0,200,100)
    'OGV1':    (0, 128, 255),    # Orange        BGR(  0,128,255)
    'OGV2':    (0, 100, 200),    # Brown/orange  BGR(  0,100,200)
    'Biker':   (255, 140, 188),  # Light blue    BGR(255,140,188)
    'Taxi':    (0, 255, 255),    # Yellow        BGR(  0,255,255)
}
DEFAULT_COLOR = (180, 180, 180)  # Gray          BGR(180,180,180)  — unknown classes

# Class groupings per regional profile — used by the legend to list the
# FULL class set of the detected profile (showing 0 for classes not seen).
PROFILE_CLASSES = {
    'India': ['Car', 'Bike', 'Truck', 'Bus', 'Auto', 'Cyclist', 'Ped'],
    'US':    ['PV', 'SU', 'CU', 'BUS', 'MC', 'PVT', 'Bicycle'],
    'UK':    ['Car', 'LGV', 'OGV1', 'OGV2', 'Bus', 'Biker', 'Cyclist', 'Taxi'],
}


def detect_profile(class_names):
    """Return the profile name whose class list best overlaps the given
    class names, or None if nothing matches (e.g. only unknown classes)."""
    best_name, best_overlap = None, 0
    for name, classes in PROFILE_CLASSES.items():
        overlap = sum(1 for c in class_names if c in classes)
        if overlap > best_overlap:
            best_name, best_overlap = name, overlap
    return best_name


# ─────────────────────────────────────────────────────────────────────────
# .traf reader
# ─────────────────────────────────────────────────────────────────────────

def read_scene_meta(conn):
    """Read the scene key/value table into a dict."""
    rows = conn.execute("SELECT key, value FROM scene").fetchall()
    return {k: v for k, v in rows}


def read_trajectories(conn, min_points=3, allowed_classes=None,
                      skip_stationary=False, per_class_cap=None, seed=42):
    """
    Return a list of (track_id, class_name, pts) tuples, where pts is a
    numpy array of shape (N, 2) of (cx, cy) ordered by frame.

    Tries `tracks.trajectory_json` first (compact polyline written by the
    exporter); falls back to per-frame `observations` if that column is
    missing or empty for a track.

    Filters:
      - allowed_classes: set of class short names to keep (None = keep all)
      - skip_stationary: drop tracks with is_stationary=1
      - per_class_cap: keep at most N trajectories per class (None = no cap).
                      When a class has more than N tracks, we prefer the
                      LONGEST ones (by point count) so the cap retains the
                      most informative trails, then break ties randomly with
                      `seed` for reproducibility.
    """
    where = []
    if skip_stationary:
        where.append("(is_stationary IS NULL OR is_stationary = 0)")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    track_rows = conn.execute(
        f"SELECT track_id, class_name, trajectory_json FROM tracks{where_sql}"
    ).fetchall()

    log.info(f"Found {len(track_rows)} tracks in .traf (after stationary filter: {skip_stationary})")

    # First pass: load every trajectory that passes class + min_points filters
    by_class = {}  # class_name -> list of (track_id, pts)
    skipped_class = 0
    skipped_short = 0

    for track_id, class_name, traj_json in track_rows:
        class_name = class_name or 'UNK'

        if allowed_classes is not None and class_name not in allowed_classes:
            skipped_class += 1
            continue

        pts = None

        # Path 1: precomputed polyline in tracks.trajectory_json
        if traj_json:
            try:
                raw = json.loads(traj_json)
                if raw and len(raw) >= min_points:
                    pts = np.array(
                        [[p[0], p[1]] for p in raw],
                        dtype=np.float32,
                    )
            except (json.JSONDecodeError, TypeError, IndexError):
                pts = None

        # Path 2: fall back to observations table
        if pts is None:
            obs = conn.execute(
                "SELECT cx, cy FROM observations "
                "WHERE track_id = ? ORDER BY frame ASC",
                (track_id,),
            ).fetchall()
            if len(obs) >= min_points:
                pts = np.array(obs, dtype=np.float32)

        if pts is None or len(pts) < min_points:
            skipped_short += 1
            continue

        by_class.setdefault(class_name, []).append((track_id, pts))

    total_before_cap = sum(len(v) for v in by_class.values())

    # Second pass: apply per-class cap
    trajectories = []
    capped_log = []
    rng = np.random.default_rng(seed)

    for class_name, items in by_class.items():
        if per_class_cap is not None and len(items) > per_class_cap:
            # Sort by trajectory length descending — longest first
            items_sorted = sorted(items, key=lambda x: -len(x[1]))

            # Take the top 60% by length deterministically, then random-sample
            # the rest. This keeps the longest, most informative trails while
            # still showing variety across short/medium ones.
            top_n = int(per_class_cap * 0.6)
            top_items = items_sorted[:top_n]

            remainder_pool = items_sorted[top_n:]
            remainder_n = per_class_cap - top_n
            if remainder_pool and remainder_n > 0:
                idxs = rng.choice(
                    len(remainder_pool),
                    size=min(remainder_n, len(remainder_pool)),
                    replace=False,
                )
                sampled = [remainder_pool[i] for i in idxs]
            else:
                sampled = []

            kept = top_items + sampled
            capped_log.append(f"{class_name}: {len(items)} → {len(kept)}")
            items = kept

        for track_id, pts in items:
            trajectories.append((track_id, class_name, pts))

    log.info(
        f"Loaded {total_before_cap} trajectories "
        f"(skipped {skipped_class} by class filter, {skipped_short} too short)"
    )
    if capped_log:
        log.info(f"Per-class cap applied: {', '.join(capped_log)}")

    # Final breakdown
    final_counts = {}
    for _, cls, _ in trajectories:
        final_counts[cls] = final_counts.get(cls, 0) + 1
    if final_counts:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(final_counts.items()))
        log.info(f"Drawing {len(trajectories)} trajectories — by class: {breakdown}")
    return trajectories


# ─────────────────────────────────────────────────────────────────────────
# Background frame
# ─────────────────────────────────────────────────────────────────────────

def grab_background_frame(video_path, frame_idx=0):
    """Read a single frame from the video to use as the canvas."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_idx < 0:
        frame_idx = 0
    if total > 0 and frame_idx >= total:
        log.warning(
            f"Requested frame {frame_idx} >= total {total}, "
            f"falling back to frame 0"
        )
        frame_idx = 0

    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ok, frame = cap.read()

    # Some codecs ignore the seek; fall back to sequential read
    if not ok:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, frame = cap.read()

    cap.release()
    if not ok or frame is None:
        raise RuntimeError(f"Cannot read frame from {video_path}")
    return frame


# ─────────────────────────────────────────────────────────────────────────
# Drawing
# ─────────────────────────────────────────────────────────────────────────

def despike_trajectories(trajectories, window=11, min_thresh_px=12.0,
                         passes=3):
    """
    Normalise jittery trajectories by removing centroid spikes.

    For each track, every point is compared against a rolling median of its
    neighbourhood (`window` points). Points that deviate by more than an
    adaptive threshold — max(min_thresh_px, 5 * median deviation of the
    track) — are spikes (single/few-frame bbox flicker) and are snapped
    back onto the rolling-median path. Point count is preserved, so frame
    alignment is unaffected. Smooth tracks pass through unchanged.

    Runs up to `passes` iterations so that longer spike runs (several
    consecutive bad frames) are also flattened: each pass shrinks the run
    from its edges. A window of 11 keeps the median clean for runs up to
    5 consecutive spiked points.
    """
    half = window // 2
    fixed_tracks = set()
    fixed_points = 0
    out = []
    for track_id, class_name, pts in trajectories:
        n = len(pts)
        if n < window:
            out.append((track_id, class_name, pts))
            continue

        work = pts
        for _ in range(passes):
            padded = np.pad(work, ((half, half), (0, 0)), mode='edge')
            win = np.lib.stride_tricks.sliding_window_view(
                padded, window_shape=window, axis=0
            )  # shape (n, 2, window)
            med = np.median(win, axis=2)  # (n, 2)

            dev = np.linalg.norm(work - med, axis=1)
            thresh = max(min_thresh_px, 5.0 * float(np.median(dev)))
            spikes = dev > thresh
            if not np.any(spikes):
                break
            work = work.copy()
            work[spikes] = med[spikes]
            fixed_tracks.add(track_id)
            fixed_points += int(spikes.sum())
        out.append((track_id, class_name, work))

    log.info(
        f"Despike: corrected {fixed_points} spike points across "
        f"{len(fixed_tracks)} tracks (window={window}, "
        f"min threshold={min_thresh_px}px, up to {passes} passes)"
    )
    return out


def _track_noise_score(pts, window=11):
    """Noise score for one track: mean deviation (px) from its own
    rolling-median path, scaled by tortuosity (path length / straight-line
    chord). Smooth direct tracks score low; wiggly/jittery tracks score
    high. Returns 0 for tracks too short to evaluate."""
    n = len(pts)
    if n < window + 2:
        return 0.0
    half = window // 2
    padded = np.pad(pts, ((half, half), (0, 0)), mode='edge')
    win = np.lib.stride_tricks.sliding_window_view(
        padded, window_shape=window, axis=0
    )
    med = np.median(win, axis=2)
    mean_dev = float(np.mean(np.linalg.norm(pts - med, axis=1)))

    seg = np.diff(pts, axis=0)
    path_len = float(np.sum(np.linalg.norm(seg, axis=1)))
    chord = float(np.linalg.norm(pts[-1] - pts[0]))
    tortuosity = path_len / max(chord, 1.0)
    return mean_dev * tortuosity


def filter_noisy_in_crowded_approaches(trajectories, keep_frac=0.7,
                                       min_keep=15, n_bins=8,
                                       protect_class_max=10,
                                       min_class_keep=3):
    """
    Reduce clutter adaptively per approach:

    - Tracks are grouped by their overall direction of travel (bearing of
      end-start displacement, binned into `n_bins` sectors). Each sector
      approximates one approach/movement direction.
    - Sparse approaches (<= min_keep tracks) are drawn in full — every
      low-volume movement stays visible.
    - Crowded approaches keep the cleanest `keep_frac` of their tracks
      (never fewer than min_keep); the noisiest are skipped from the plot.

    RARE-CLASS PROTECTION: classes with <= `protect_class_max` tracks in
    total are NEVER skipped — if the file has one BUS, it is always drawn,
    so the viewer never gets the impression that a class wasn't present.
    Additionally, every class retains at least `min_class_keep` tracks
    (or all of them, if fewer exist): if filtering would leave a class
    under-represented, its cleanest skipped tracks are restored.

    Near-stationary tracks (chord < 30 px) get their own group, since
    their bearing is meaningless. Display-only: nothing is removed from
    the .traf.
    """
    if keep_frac >= 1.0:
        return trajectories

    # Total tracks per class -> which classes are rare (fully protected)
    class_totals = {}
    for _tid, cls, _pts in trajectories:
        class_totals[cls] = class_totals.get(cls, 0) + 1
    protected_classes = {
        c for c, n in class_totals.items() if n <= protect_class_max
    }
    if protected_classes:
        log.info(
            f"Approach filter: rare classes fully protected from "
            f"skipping: { {c: class_totals[c] for c in protected_classes} }"
        )

    # Score + bin every track
    binned = {}  # bin index (or 'stationary') -> list of (score, traj)
    for traj in trajectories:
        _tid, _cls, pts = traj
        disp = pts[-1] - pts[0]
        chord = float(np.hypot(disp[0], disp[1]))
        if chord < 30.0:
            key = 'stationary'
        else:
            ang = np.arctan2(disp[1], disp[0])  # -pi..pi
            # Round to nearest sector so cardinal directions (E/N/W/S...)
            # sit at bin CENTERS, not on bin edges — otherwise one approach
            # straddling a boundary splits into two half-sized groups.
            key = int(np.round(ang / (2 * np.pi / n_bins))) % n_bins
        binned.setdefault(key, []).append((_track_noise_score(pts), traj))

    out = []
    dropped_all = []  # (score, traj) skipped, for per-class restoration
    for key, items in binned.items():
        n = len(items)
        if n <= min_keep:
            out.extend(t for _s, t in items)
            log.info(f"Approach filter: bin {key}: {n} tracks (sparse, kept all)")
            continue

        # Rare-class tracks bypass the cut entirely
        exempt = [(s, t) for s, t in items if t[1] in protected_classes]
        candidates = [(s, t) for s, t in items if t[1] not in protected_classes]
        out.extend(t for _s, t in exempt)

        k = max(min_keep - len(exempt), int(round(len(candidates) * keep_frac)))
        candidates.sort(key=lambda st: st[0])  # cleanest first
        kept, dropped = candidates[:k], candidates[k:]
        out.extend(t for _s, t in kept)
        dropped_all.extend(dropped)
        log.info(
            f"Approach filter: bin {key}: kept {len(kept) + len(exempt)}/{n} "
            f"({len(exempt)} rare-class protected), "
            f"skipped {len(dropped)} noisiest"
        )

    # Per-class floor: every class keeps at least min_class_keep tracks
    # (or its full total if smaller). Restore cleanest skipped if needed.
    kept_per_class = {}
    for _tid, cls, _pts in out:
        kept_per_class[cls] = kept_per_class.get(cls, 0) + 1
    dropped_all.sort(key=lambda st: st[0])  # cleanest first
    restored = 0
    for s, traj in list(dropped_all):
        cls = traj[1]
        floor = min(class_totals[cls], min_class_keep)
        if kept_per_class.get(cls, 0) < floor:
            out.append(traj)
            dropped_all.remove((s, traj))
            kept_per_class[cls] = kept_per_class.get(cls, 0) + 1
            restored += 1
    if restored:
        log.info(
            f"Approach filter: restored {restored} tracks to keep every "
            f"class represented (min {min_class_keep} per class)"
        )

    log.info(
        f"Approach filter: skipped {len(dropped_all)} noisy tracks total; "
        f"{len(out)}/{len(trajectories)} remain; "
        f"kept per class: {kept_per_class}"
    )
    return out


def smooth_trajectories(trajectories, sigma=2.0):
    """
    Gaussian-smooth each trajectory to remove per-point centroid wiggle
    (bbox quantisation noise), giving clean flowing lines à la DataFromSky.

    Each coordinate is convolved with a Gaussian kernel (std = `sigma`
    points, edge-padded so endpoints stay anchored). Unlike despiking —
    which only fixes outlier points — this softens EVERY point slightly,
    so high-frequency wiggle disappears while curves/turns (low-frequency
    shape) are preserved. sigma <= 0 disables. Display-only.
    """
    if sigma <= 0:
        return trajectories

    radius = max(1, int(np.ceil(3 * sigma)))
    kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    kernel /= kernel.sum()

    out = []
    smoothed = 0
    for track_id, class_name, pts in trajectories:
        if len(pts) < 2 * radius + 1:
            out.append((track_id, class_name, pts))
            continue
        padded = np.pad(pts, ((radius, radius), (0, 0)), mode='edge')
        sm = np.empty_like(pts, dtype=float)
        sm[:, 0] = np.convolve(padded[:, 0], kernel, mode='valid')
        sm[:, 1] = np.convolve(padded[:, 1], kernel, mode='valid')
        out.append((track_id, class_name, sm))
        smoothed += 1

    log.info(f"Smoothing: gaussian sigma={sigma} applied to {smoothed} tracks")
    return out


def trim_departing_starts(trajectories, frame_h, n_trim,
                          bottom_frac=0.30, min_keep=10):
    """
    Remove the first `n_trim` points of trajectories that are MOVING AWAY
    from the camera, i.e. tracks that START in the bottom `bottom_frac` of
    the frame and travel upward (image y decreasing overall).

    Rationale: departing vehicles enter the frame closest to the camera,
    where the bbox is huge and partially clipped — the first few centroids
    jitter badly (the white mess at the frame's bottom edge). Approaching
    tracks are left untouched: their START is far away and clean.

    A track is only trimmed if at least `min_keep` points remain.
    """
    if n_trim <= 0:
        return trajectories

    out = []
    trimmed = 0
    bottom_y = frame_h * (1.0 - bottom_frac)
    for track_id, class_name, pts in trajectories:
        if len(pts) >= n_trim + min_keep:
            # Robust start/end y: median of first/last 5 points
            k = min(5, len(pts))
            start_y = float(np.median(pts[:k, 1]))
            end_y = float(np.median(pts[-k:, 1]))
            starts_near_camera = start_y >= bottom_y
            moving_away = end_y < start_y - 0.05 * frame_h
            if starts_near_camera and moving_away:
                pts = pts[n_trim:]
                trimmed += 1
        out.append((track_id, class_name, pts))

    log.info(
        f"Trimmed first {n_trim} points of {trimmed} departing tracks "
        f"(start below y={bottom_y:.0f}, moving away from camera)"
    )
    return out


def _gradient_color(class_color, t, start_whiteness=0.78):
    """Blend from a near-white tint of the class color (t=0, track origin)
    to the full class color (t=1, track end). BGR in, BGR out."""
    a = (1.0 - start_whiteness) + start_whiteness * t  # 0.22 → 1.0
    return tuple(int(255 * (1.0 - a) + c * a) for c in class_color)


def draw_trajectories(canvas, trajectories, thickness, taper=False,
                      gradient=True):
    """
    Draw every trajectory as a polyline on the canvas in-place.
    Color is per-class (CLASS_COLORS lookup).

    gradient=True (default): DataFromSky-style — thin line that starts as a
    light, near-white tint at the track origin and blends into the full
    class color as the track progresses. The white tip = where the vehicle
    entered the scene. Uniform `thickness` px (use 1 for thin lines).

    taper=True (only used when gradient=False): solid class color, 1 px at
    origin growing to `thickness` px (min 3) at the end.

    Both off: uniform solid polyline (original behavior).

    Pedestrians are drawn LAST so they sit on top of vehicle trails (matches
    last-year's deliverable style where ped trails are the focus).
    """
    h, w = canvas.shape[:2]

    # Sort: vehicles first, peds last (drawn on top)
    def _ped_last_key(item):
        cls = item[1]
        return (1 if cls in ('Ped', 'Pedestrian', 'Peds') else 0)

    trajectories_sorted = sorted(trajectories, key=_ped_last_key)

    # Max thickness for taper: at least 3 so the growth is visible even
    # when --thickness 1 is used.
    max_th = max(thickness, 3) if taper else thickness

    GRADIENT_BANDS = 24  # color steps per track — smooth but fast

    drawn = 0
    drawn_by_class = {}
    for _track_id, class_name, pts in trajectories_sorted:
        in_bounds = (
            (pts[:, 0] >= 0)
            & (pts[:, 0] < w)
            & (pts[:, 1] >= 0)
            & (pts[:, 1] < h)
        )
        if not np.any(in_bounds):
            continue

        color = CLASS_COLORS.get(class_name, DEFAULT_COLOR)
        poly = pts.astype(np.int32)
        n_seg = len(poly) - 1

        if gradient and n_seg >= 2:
            # Split the track into bands; each band gets a color blended
            # from near-white (origin) to the full class color (end).
            n_bands = min(GRADIENT_BANDS, n_seg)
            # Band boundaries as point indices (inclusive overlap so bands
            # connect seamlessly).
            bounds = np.linspace(0, n_seg, n_bands + 1).round().astype(int)
            for b in range(n_bands):
                p0, p1 = bounds[b], bounds[b + 1]
                if p1 <= p0:
                    continue
                t_mid = (b + 0.5) / n_bands
                band_color = _gradient_color(color, t_mid)
                band = poly[p0:p1 + 1].reshape(-1, 1, 2)
                cv2.polylines(
                    canvas, [band], isClosed=False, color=band_color,
                    thickness=thickness, lineType=cv2.LINE_AA,
                )
        elif taper and n_seg >= 2:
            # Assign each segment a thickness from 1 → max_th along the
            # track, then draw runs of equal thickness as one polyline
            # (much faster than per-segment cv2.line on long tracks).
            seg_th = 1 + np.round(
                np.arange(n_seg) / max(n_seg - 1, 1) * (max_th - 1)
            ).astype(int)
            start = 0
            for i in range(1, n_seg + 1):
                if i == n_seg or seg_th[i] != seg_th[start]:
                    # points start..i inclusive form this thickness band
                    band = poly[start:i + 1].reshape(-1, 1, 2)
                    cv2.polylines(
                        canvas, [band], isClosed=False, color=color,
                        thickness=int(seg_th[start]), lineType=cv2.LINE_AA,
                    )
                    start = i
        else:
            cv2.polylines(
                canvas,
                [poly.reshape(-1, 1, 2)],
                isClosed=False,
                color=color,
                thickness=thickness,
                lineType=cv2.LINE_AA,
            )

        drawn += 1
        drawn_by_class[class_name] = drawn_by_class.get(class_name, 0) + 1

    mode = ('gradient (white->class color)' if gradient
            else f'taper 1->{max_th}px' if taper else 'plain')
    log.info(
        f"Drew {drawn} polylines (mode={mode}, thickness={thickness}px); "
        f"per-class counts: {drawn_by_class}"
    )
    return canvas, drawn_by_class


def draw_job_info(canvas, job_number=None, site_name=None):
    """Draw a job-details panel in the top-RIGHT corner (semi-transparent
    dark box, same style as the class legend). Shows whichever of
    job number / site name were provided; skips silently if neither."""
    lines = []
    if job_number:
        lines.append(f"Job: {job_number}")
    if site_name:
        lines.append(f"Site: {site_name}")
    if not lines:
        return canvas

    pad = 10
    line_h = 22
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.5
    font_thickness = 1

    text_widths = [
        cv2.getTextSize(t, font, font_scale, font_thickness)[0][0]
        for t in lines
    ]
    panel_w = pad * 2 + max(text_widths)
    panel_h = pad + line_h * len(lines) + pad

    # Anchor to top-right
    W = canvas.shape[1]
    x0 = W - panel_w - pad // 2
    y0 = pad // 2

    overlay = canvas.copy()
    cv2.rectangle(
        overlay, (x0, y0), (x0 + panel_w, y0 + panel_h),
        (30, 30, 30), thickness=-1,
    )
    cv2.addWeighted(overlay, 0.7, canvas, 0.3, 0, canvas)

    y = y0 + pad + 11
    for t in lines:
        cv2.putText(
            canvas, t, (x0 + pad, y + 5),
            font, font_scale, (240, 240, 240),
            font_thickness, lineType=cv2.LINE_AA,
        )
        y += line_h

    log.info(f"Job info panel: {lines} at top-right ({panel_w}x{panel_h}px)")
    return canvas


def draw_legend(canvas, drawn_by_class):
    """Draw a small color-coded legend in the top-left corner.

    Each entry is a colored dot followed by "Class (count)", e.g. "PV (430)".
    """
    if not drawn_by_class:
        log.warning("LEGEND DEBUG: drawn_by_class is empty — nothing was drawn, skipping legend")
        return canvas

    # Expand to the full class list of the detected profile, with 0 counts
    # for classes that were not drawn. Unknown classes (not in any profile)
    # are kept as-is.
    profile = detect_profile(drawn_by_class.keys())
    full_counts = {}
    if profile is not None:
        # Order entries by the profile's class list (config-defined sequence),
        # showing 0 for classes not drawn. Any classes present in the data but
        # not in the profile list are appended afterwards in their drawn order.
        for cls in PROFILE_CLASSES[profile]:
            full_counts[cls] = drawn_by_class.get(cls, 0)
        for cls, cnt in drawn_by_class.items():
            if cls not in full_counts:
                full_counts[cls] = cnt
        log.info(f"LEGEND DEBUG: detected profile '{profile}', "
                 f"showing all {len(PROFILE_CLASSES[profile])} classes "
                 f"in profile order")
    else:
        full_counts = dict(drawn_by_class)

    log.info(f"LEGEND DEBUG: drawing legend with entries: {dict(full_counts)}")

    pad = 10            # outer margin of the legend box
    line_h = 22         # vertical spacing between entries
    dot_radius = 5      # radius of the colored dot
    dot_cx_off = pad + dot_radius     # dot center x, relative to box origin
    text_x_off = pad + dot_radius * 2 + 8  # label x, relative to box origin

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.5
    font_thickness = 1

    # Keep profile-list order (set in full_counts above); build labels.
    entries = list(full_counts.items())
    labels = [f"{cls} ({cnt})" for cls, cnt in entries]
    text_widths = [
        cv2.getTextSize(lbl, font, font_scale, font_thickness)[0][0]
        for lbl in labels
    ]

    legend_w = text_x_off + max(text_widths) + pad
    legend_h = pad + line_h * len(entries) + pad

    log.info(
        f"LEGEND DEBUG: box at top-left (x={pad // 2}, y={pad // 2}), "
        f"size {legend_w}x{legend_h}px on canvas {canvas.shape[1]}x{canvas.shape[0]}"
    )

    # Box origin (top-left corner of the legend panel)
    x0, y0 = pad // 2, pad // 2

    # Semi-transparent dark background
    overlay = canvas.copy()
    cv2.rectangle(
        overlay,
        (x0, y0),
        (x0 + legend_w, y0 + legend_h),
        (30, 30, 30),
        thickness=-1,
    )
    cv2.addWeighted(overlay, 0.7, canvas, 0.3, 0, canvas)

    # Entries — dot + "Class (count)"
    y = y0 + pad + dot_radius
    for (cls, _cnt), label in zip(entries, labels):
        color = CLASS_COLORS.get(cls, DEFAULT_COLOR)
        cv2.circle(
            canvas,
            (x0 + dot_cx_off, y),
            dot_radius,
            color,
            thickness=-1,
            lineType=cv2.LINE_AA,
        )
        cv2.putText(
            canvas, label, (x0 + text_x_off, y + 5),
            font, font_scale, (240, 240, 240),
            font_thickness, lineType=cv2.LINE_AA,
        )
        y += line_h

    return canvas


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

def parse_class_list(s):
    if not s:
        return None
    return [c.strip() for c in str(s).split(",") if c.strip()]


def render_trajectory_plot(traf_path, video_path, out_path,
                           classes=None, per_class=None, min_points=10,
                           skip_stationary=True, thickness=1, legend=True,
                           frame=0, seed=42, despike=True, despike_px=12.0,
                           smooth_sigma=2.0, trim_departing=6, keep_frac=0.7,
                           min_per_approach=15, protect_class_max=10,
                           gradient=True, taper=False,
                           job_number=None, site_name=None, jpg=False,
                           track_ids=None, frame_from=None, frame_to=None,
                           background=None):
    """Render all trajectories from a .traf onto a background frame.

    Importable equivalent of the original trajectory_plot.py CLI —
    identical processing chain: read → despike → smooth → trim →
    clutter-filter → draw → legend → job panel → save.
    Returns dict with out_path, drawn/file class counts.
    """
    if not os.path.isfile(traf_path):
        raise FileNotFoundError(f".traf not found: {traf_path}")
    if background is None and not os.path.isfile(video_path or ''):
        raise FileNotFoundError(f"video not found: {video_path}")

    allowed = parse_class_list(classes) if isinstance(classes, str) else classes

    conn = sqlite3.connect(traf_path)
    try:
        meta = read_scene_meta(conn)
        trajectories = read_trajectories(
            conn, min_points=min_points, allowed_classes=allowed,
            skip_stationary=skip_stationary, per_class_cap=per_class,
            seed=seed)

        # Optional: restrict to specific tracks (e.g. a gate→gate movement)
        if track_ids is not None:
            keep = set(int(t) for t in track_ids)
            trajectories = [t for t in trajectories if int(t[0]) in keep]

        # Optional: restrict to a frame window (tracks overlapping it)
        if frame_from is not None or frame_to is not None:
            spans = dict(conn.execute(
                "SELECT track_id, first_frame || ',' || last_frame FROM tracks"))
            lo = frame_from if frame_from is not None else -1
            hi = frame_to if frame_to is not None else float('inf')
            def _overlaps(tid):
                s = spans.get(tid) or spans.get(int(tid))
                if not s:
                    return True
                f0, f1 = (int(x) for x in s.split(','))
                return f1 >= lo and f0 <= hi
            trajectories = [t for t in trajectories if _overlaps(t[0])]
    finally:
        conn.close()

    if not trajectories:
        raise ValueError("No trajectories to draw with the given filters.")

    canvas = (background.copy() if background is not None
              else grab_background_frame(video_path, frame_idx=frame))

    file_class_counts = {}
    for _tid, _cls, _pts in trajectories:
        file_class_counts[_cls] = file_class_counts.get(_cls, 0) + 1

    if despike:
        trajectories = despike_trajectories(trajectories,
                                            min_thresh_px=despike_px)
    trajectories = smooth_trajectories(trajectories, sigma=smooth_sigma)
    trajectories = trim_departing_starts(
        trajectories, frame_h=canvas.shape[0], n_trim=trim_departing)
    trajectories = filter_noisy_in_crowded_approaches(
        trajectories, keep_frac=keep_frac, min_keep=min_per_approach,
        protect_class_max=protect_class_max)

    use_gradient = gradient and not taper
    canvas, drawn_by_class = draw_trajectories(
        canvas, trajectories, thickness, taper=taper, gradient=use_gradient)

    if legend:
        canvas = draw_legend(canvas, file_class_counts)
    canvas = draw_job_info(canvas, job_number=job_number, site_name=site_name)

    if jpg:
        root, _ = os.path.splitext(out_path)
        out_path = root + ".jpg"
        ok = cv2.imwrite(out_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 95])
    else:
        ok = cv2.imwrite(out_path, canvas)
    if not ok:
        raise IOError(f"Failed to write image to {out_path}")

    return {"out_path": out_path,
            "file_class_counts": file_class_counts,
            "drawn_by_class": drawn_by_class,
            "meta": meta}
