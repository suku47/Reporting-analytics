"""Near-miss detection engine (viewer core).

Improved PET methodology over the standalone analyzer:
  1. PET interpolated on the intersecting segments (sub-frame accurate,
     immune to the twice-past-the-point argmin bug)
  2. Conflict distance scaled by bbox diagonal (depth-aware: "within half
     a vehicle length" means the same thing near and far)
  3. Stationary tracks and far-field noise excluded
  4. Severity buckets from PET x approach speed
  5. Mode-aware angle filter (veh-ped keeps parallel filter; veh-veh keeps
     rear-end conflicts via closing-speed test)
  6. TTC-lite pass: converging-but-never-crossing conflicts (braked/
     swerved avoidances) that pure PET cannot see
"""

import json
import logging
import sqlite3

import cv2
import numpy as np

log = logging.getLogger(__name__)

PED_LIKE = {'Ped', 'Cyclist', 'P/CYCLE', 'Bike', 'Bicycle'}


def load_tracks(traf_path):
    conn = sqlite3.connect(traf_path)
    tracks = {}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
    stat_col = 'is_stationary' if 'is_stationary' in cols else "0"
    spd_col = 'speed_mean' if 'speed_mean' in cols else 'avg_speed'         if 'avg_speed' in cols else "0"
    for tid, cls, cfull, tj, stat, spd in conn.execute(
            f"SELECT track_id, class_name, class_full_name, trajectory_json, "
            f"{stat_col}, {spd_col} FROM tracks"):
        try:
            pts = json.loads(tj)
        except Exception:
            continue
        if len(pts) < 5:
            continue
        pos = np.array([[p[0], p[1]] for p in pts], dtype=np.float32)
        frames = np.array([p[2] for p in pts], dtype=np.float64)
        diag = None
        if len(pts[0]) >= 4:
            pass
        tracks[tid] = {'pos': pos, 'frames': frames, 'cls': cls or 'PV',
                       'cls_full': cfull or cls or 'Vehicle',
                       'stationary': bool(stat), 'speed_mean': spd or 0.0}
    # per-track bbox diagonal proxy from observations (median)
    for tid, d0, d1 in conn.execute(
            "SELECT track_id, AVG(bbox_x2-bbox_x1), AVG(bbox_y2-bbox_y1) "
            "FROM observations GROUP BY track_id"):
        if tid in tracks and d0 and d1:
            tracks[tid]['diag'] = float((d0 * d0 + d1 * d1) ** 0.5)
    for t in tracks.values():
        t.setdefault('diag', 60.0)
    conn.close()
    return tracks


def _seg_intersect(a1, a2, b1, b2):
    d1 = a2 - a1
    d2 = b2 - b1
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-9:
        return None
    dx, dy = b1[0] - a1[0], b1[1] - a1[1]
    ua = (dx * d2[1] - dy * d2[0]) / den
    ub = (dx * d1[1] - dy * d1[0]) / den
    if 0 <= ua <= 1 and 0 <= ub <= 1:
        return ua, ub, a1 + ua * d1
    return None


def _heading(pos, i, w=3):
    a = max(0, i - w)
    b = min(len(pos) - 1, i + w)
    v = pos[b] - pos[a]
    n = np.linalg.norm(v)
    return None if n < 2 else float(np.degrees(np.arctan2(v[1], v[0])))


def _speed_at(pos, frames, i, w=3):
    a = max(0, i - w)
    b = min(len(pos) - 1, i + w)
    df = frames[b] - frames[a]
    return float(np.linalg.norm(pos[b] - pos[a]) / df) if df > 0 else 0.0


def severity(pet, speed_pxf, fps):
    """Buckets from PET x speed. Speed normalized to px/s for scale."""
    sp = speed_pxf * fps
    if pet is None:                       # TTC event
        return 'moderate'
    if pet < 0.5 and sp > 60:
        return 'critical'
    if pet < 1.0 and sp > 40:
        return 'severe'
    if pet < 2.0:
        return 'moderate'
    return 'slight'


def detect(traf_path, mode='veh_ped', pet_threshold=3.0, diag_factor=0.6,
           min_angle=15.0, ttc_threshold=1.5, min_diag=45.0, fps=None):
    tracks = load_tracks(traf_path)
    if fps is None:
        c = sqlite3.connect(traf_path)
        meta = dict(c.execute("SELECT key, value FROM scene"))
        c.close()
        fps = float(meta.get('fps', 30.0))

    # guard 3: drop stationary + far-field noise tracks
    usable = {tid: t for tid, t in tracks.items()
              if not t['stationary'] and t['diag'] >= min_diag}

    peds = {tid: t for tid, t in usable.items() if t['cls'] in PED_LIKE}
    vehs = {tid: t for tid, t in usable.items() if t['cls'] not in PED_LIKE}
    if mode == 'veh_ped':
        A, B, same = vehs, peds, False
    else:
        A, B, same = vehs, vehs, True

    events = []
    a_ids = list(A.keys())
    b_ids = list(B.keys())
    pet_frames = pet_threshold * fps

    for ai, aid in enumerate(a_ids):
        ta = A[aid]
        pa, fa = ta['pos'], ta['frames']
        abox = (pa[:, 0].min(), pa[:, 1].min(), pa[:, 0].max(), pa[:, 1].max())
        for bid in (a_ids[ai + 1:] if same else b_ids):
            tb = B[bid]
            pb, fb = tb['pos'], tb['frames']
            margin = diag_factor * max(ta['diag'], tb['diag'])
            # spatial prefilter
            if (pb[:, 0].max() < abox[0] - margin or
                    pb[:, 0].min() > abox[2] + margin or
                    pb[:, 1].max() < abox[1] - margin or
                    pb[:, 1].min() > abox[3] + margin):
                continue
            # temporal prefilter
            if fb[-1] < fa[0] - pet_frames or fb[0] > fa[-1] + pet_frames:
                continue

            best = None
            # ── PET via exact segment intersection + interpolation ──
            for k in range(len(pa) - 1):
                for l in range(len(pb) - 1):
                    hit = _seg_intersect(pa[k], pa[k + 1], pb[l], pb[l + 1])
                    if hit is None:
                        continue
                    ua, ub, pt = hit
                    fA = fa[k] + ua * (fa[k + 1] - fa[k])
                    fB = fb[l] + ub * (fb[l + 1] - fb[l])
                    pet = abs(fA - fB) / fps
                    if pet > pet_threshold:
                        continue
                    va = _speed_at(pa, fa, k)
                    vb = _speed_at(pb, fb, l)
                    ha = _heading(pa, k)
                    hb = _heading(pb, l)
                    if ha is not None and hb is not None:
                        ad = abs(ha - hb)
                        ad = 360 - ad if ad > 180 else ad
                        if ad < min_angle:
                            if not same:
                                continue          # veh-ped parallel: skip
                            # veh-veh low angle: keep only if closing fast
                            gap = float(np.linalg.norm(pa[k] - pb[l]))
                            rel = abs(va - vb)
                            if rel < 1e-3 or gap / max(rel, 1e-3) / fps > ttc_threshold * 2:
                                continue
                    else:
                        ad = None
                    if best is None or pet < best['pet']:
                        spd = max(va, vb)
                        best = {'type': 'PET', 'a_id': int(aid), 'b_id': int(bid),
                                'point': [float(pt[0]), float(pt[1])],
                                'pet': round(pet, 2), 'ttc': None,
                                'frame': int((fA + fB) / 2),
                                'angle': None if ad is None else round(ad, 1),
                                'speed': round(spd, 2),
                                'a_cls': ta['cls'], 'b_cls': tb['cls'],
                                'severity': severity(pet, spd, fps)}
            # ── TTC-lite: converging but never crossing ──
            if best is None:
                f0 = max(fa[0], fb[0])
                f1 = min(fa[-1], fb[-1])
                if f1 > f0:
                    samples = np.arange(f0, f1, max(3, int(fps / 6)))
                    ia = np.searchsorted(fa, samples).clip(0, len(pa) - 1)
                    ib = np.searchsorted(fb, samples).clip(0, len(pb) - 1)
                    d = np.linalg.norm(pa[ia] - pb[ib], axis=1)
                    if len(d) > 2:
                        closing = -(np.diff(d) /
                                    np.maximum(np.diff(samples), 1e-6))  # px/f
                        with np.errstate(divide='ignore', invalid='ignore'):
                            ttc = np.where(closing > 0.3,
                                           d[1:] / closing / fps, np.inf)
                        j = int(np.argmin(ttc))
                        near_enough = d[j + 1] < margin * 2
                        if ttc[j] < ttc_threshold and near_enough:
                            mid = (pa[ia[j + 1]] + pb[ib[j + 1]]) / 2
                            spd = _speed_at(pa, fa, int(ia[j + 1]))
                            best = {'type': 'TTC', 'a_id': int(aid),
                                    'b_id': int(bid),
                                    'point': [float(mid[0]), float(mid[1])],
                                    'pet': None, 'ttc': round(float(ttc[j]), 2),
                                    'frame': int(samples[j + 1]),
                                    'angle': None, 'speed': round(spd, 2),
                                    'a_cls': ta['cls'], 'b_cls': tb['cls'],
                                    'severity': severity(None, spd, fps)}
            if best is not None:
                events.append(best)

    order = {'critical': 0, 'severe': 1, 'moderate': 2, 'slight': 3}
    events.sort(key=lambda e: (order[e['severity']],
                               e['pet'] if e['pet'] is not None else e['ttc']))
    log.info(f"near-miss: {len(events)} conflicts "
             f"({len(vehs)} veh, {len(peds)} ped, mode={mode})")
    return events


SEV_COLOR = {'critical': (60, 60, 255), 'severe': (60, 140, 255),
             'moderate': (60, 200, 255), 'slight': (160, 200, 160)}


def render_conflict_map(traf_path, events, out_path, focus=None):
    """Conflict points on the clean background; focus=(a_id,b_id) draws
    that pair's two trajectories highlighted."""
    from app.core.background_frame import load_stored_background
    canvas = load_stored_background(traf_path)
    if canvas is None:
        raise FileNotFoundError("no stored background frame in this .traf")
    canvas = canvas.copy()
    if focus:
        tracks = load_tracks(traf_path)
        for tid, col in ((focus[0], (255, 200, 60)), (focus[1], (60, 120, 255))):
            t = tracks.get(int(tid))
            if t is None:
                continue
            pts = t['pos'].astype(int)
            for i in range(1, len(pts)):
                cv2.line(canvas, tuple(pts[i - 1]), tuple(pts[i]), col, 2,
                         cv2.LINE_AA)
    for e in events:
        p = (int(e['point'][0]), int(e['point'][1]))
        col = SEV_COLOR[e['severity']]
        r = {'critical': 14, 'severe': 11, 'moderate': 8, 'slight': 6}[e['severity']]
        cv2.circle(canvas, p, r, col, -1, cv2.LINE_AA)
        cv2.circle(canvas, p, r, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.imwrite(out_path, canvas)
    return out_path
