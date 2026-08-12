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


def load_tracks(traf_path, geometry='ground'):
    """Load per-track trajectories for conflict analysis.

    geometry='ground' (default): bbox bottom-center from observations —
    the road contact point. Centroids of tall vehicles (trucks, buses)
    ride high in the image and their trajectory lines sweep across the
    oncoming lane, creating phantom image-space crossings between
    opposite-direction traffic that never shared road space. Ground
    points of lane-separated traffic trace parallel curves and don't
    intersect, while genuine crossings (turns across traffic, pedestrian
    crossings) still do.

    geometry='centroid': legacy behavior (trajectory_json), kept for
    comparison runs and as fallback when observations are missing.
    """
    conn = sqlite3.connect(traf_path)
    tracks = {}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
    stat_col = 'is_stationary' if 'is_stationary' in cols else "0"
    spd_col = 'speed_mean' if 'speed_mean' in cols else 'avg_speed'         if 'avg_speed' in cols else "0"

    # ── ground points: bottom-center per frame, subsampled every 3rd ──
    ground = {}
    if geometry == 'ground':
        try:
            for tid, frame, x1, x2, y2 in conn.execute(
                    "SELECT track_id, frame, bbox_x1, bbox_x2, bbox_y2 "
                    "FROM observations WHERE bbox_x1 IS NOT NULL "
                    "ORDER BY track_id, frame"):
                ground.setdefault(tid, []).append(
                    ((x1 + x2) / 2.0, y2, frame))
            for tid in ground:
                ground[tid] = ground[tid][::3]      # every 3rd frame
        except sqlite3.OperationalError:
            ground = {}                              # old traf w/o obs

    for tid, cls, cfull, tj, stat, spd in conn.execute(
            f"SELECT track_id, class_name, class_full_name, trajectory_json, "
            f"{stat_col}, {spd_col} FROM tracks"):
        pts = ground.get(tid)
        if not pts:                                  # fallback: centroids
            try:
                pts = json.loads(tj)
            except Exception:
                continue
        if len(pts) < 5:
            continue
        pos = np.array([[p[0], p[1]] for p in pts], dtype=np.float32)
        frames = np.array([p[2] for p in pts], dtype=np.float64)
        tracks[tid] = {'pos': pos, 'frames': frames, 'cls': cls or 'PV',
                       'cls_full': cfull or cls or 'Vehicle',
                       'stationary': bool(stat), 'speed_mean': spd or 0.0,
                       'geometry': 'ground' if tid in ground else 'centroid'}
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
           min_angle=15.0, ttc_threshold=1.5, min_diag=45.0, fps=None,
           debug=True, enable_ttc=False):
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

    # ── debug run record: written to <traf_dir>/nearmiss_debug.json ──
    import datetime as _dt
    n_ground = sum(1 for t in tracks.values()
                   if t.get('geometry') == 'ground')
    dbg = {
        'run': {'timestamp': _dt.datetime.now().isoformat(timespec='seconds'),
                'engine': 'v2.4-pet-only' if not enable_ttc else 'v2.4-pet+ttc',
                'enable_ttc': enable_ttc,
                'geometry': f'{n_ground}/{len(tracks)} tracks ground-point',
                'traf': traf_path, 'mode': mode, 'fps': fps,
                'pet_threshold': pet_threshold, 'ttc_threshold': ttc_threshold,
                'min_angle': min_angle, 'diag_factor': diag_factor,
                'min_diag': min_diag,
                'tracks_total': len(tracks), 'tracks_usable': len(usable),
                'dropped_stationary': sum(1 for t in tracks.values()
                                          if t['stationary']),
                'dropped_small_diag': sum(1 for t in tracks.values()
                                          if not t['stationary']
                                          and t['diag'] < min_diag)},
        'counters': {'pairs_considered': 0, 'spatial_reject': 0,
                     'temporal_reject': 0, 'pet_hits': 0,
                     'pet_over_threshold': 0, 'pet_parallel_skip': 0,
                     'ttc_hits': 0, 'ttc_parallel_veto': 0,
                     'ttc_head_on_veto': 0, 'ttc_never_near_veto': 0,
                     'ttc_not_near': 0, 'ttc_no_sustained_closing': 0},
        'events': [], 'ttc_vetoed_candidates': []}
    C = dbg['counters']

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
            C['pairs_considered'] += 1
            # spatial prefilter
            if (pb[:, 0].max() < abox[0] - margin or
                    pb[:, 0].min() > abox[2] + margin or
                    pb[:, 1].max() < abox[1] - margin or
                    pb[:, 1].min() > abox[3] + margin):
                C['spatial_reject'] += 1
                continue
            # temporal prefilter
            if fb[-1] < fa[0] - pet_frames or fb[0] > fa[-1] + pet_frames:
                C['temporal_reject'] += 1
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
                        C['pet_over_threshold'] += 1
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
                                C['pet_parallel_skip'] += 1
                                continue          # veh-ped parallel: skip
                            # veh-veh low angle: keep only if closing fast
                            gap = float(np.linalg.norm(pa[k] - pb[l]))
                            rel = abs(va - vb)
                            if rel < 1e-3 or gap / max(rel, 1e-3) / fps > ttc_threshold * 2:
                                C['pet_parallel_skip'] += 1
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
                                'severity': severity(pet, spd, fps),
                                'dbg': {'fA': round(float(fA), 1),
                                        'fB': round(float(fB), 1),
                                        'heading_a': None if ha is None else round(ha, 1),
                                        'heading_b': None if hb is None else round(hb, 1),
                                        'speed_a_pxs': round(va * fps, 1),
                                        'speed_b_pxs': round(vb * fps, 1),
                                        'margin_px': round(margin, 1),
                                        'diag_a': round(ta['diag'], 1),
                                        'diag_b': round(tb['diag'], 1)}}
            # ── TTC-lite v3: closest-point-of-approach (CPA) ──
            # Positions interpolated onto a common timeline; at each sample
            # extrapolate constant velocities and compute when/how close the
            # pair would pass (t*, d_min). Only genuinely-converging-to-
            # contact pairs fire. Vetoes (all logged for review):
            #   parallel  (angle < min_angle)        → car-following/overtake
            #   head_on   (angle > 180 - 2*min_angle) → opposing lanes passing
            if best is None and enable_ttc:
                f0 = max(fa[0], fb[0])
                f1 = min(fa[-1], fb[-1])
                samples = np.arange(f0, f1, max(3, int(fps / 6)))
                if len(samples) > 4:
                    Ps_a = np.stack([np.interp(samples, fa, pa[:, 0]),
                                     np.interp(samples, fa, pa[:, 1])], axis=1)
                    Ps_b = np.stack([np.interp(samples, fb, pb[:, 0]),
                                     np.interp(samples, fb, pb[:, 1])], axis=1)
                    dt = np.diff(samples)
                    vA = np.diff(Ps_a, axis=0) / dt[:, None]   # px/frame
                    vB = np.diff(Ps_b, axis=0) / dt[:, None]
                    # light smoothing on velocities (bbox jitter)
                    if len(vA) > 3:
                        k = np.ones(3) / 3.0
                        vA = np.stack([np.convolve(vA[:, 0], k, 'same'),
                                       np.convolve(vA[:, 1], k, 'same')], 1)
                        vB = np.stack([np.convolve(vB[:, 0], k, 'same'),
                                       np.convolve(vB[:, 1], k, 'same')], 1)
                    r = Ps_b[1:] - Ps_a[1:]
                    v = vB - vA
                    vv = (v * v).sum(1)
                    with np.errstate(divide='ignore', invalid='ignore'):
                        tstar = np.where(vv > 1e-6,
                                         -(r * v).sum(1) / np.maximum(vv, 1e-6),
                                         np.inf)
                    dmin = np.linalg.norm(
                        r + v * np.clip(tstar, 0, 1e6)[:, None], axis=1)
                    # acceptance radius from AVERAGE vehicle size (max-diag
                    # let one trailer truck inflate its pairs' radius)
                    conflict_dist = 0.3 * (ta['diag'] + tb['diag']) / 2.0
                    # actual-proximity: how close did the pair REALLY get?
                    # Predicted collisions between vehicles that never came
                    # near each other are curve-following artifacts, not
                    # avoidance events.
                    dact = np.linalg.norm(Ps_a - Ps_b, axis=1)
                    min_dact = float(dact.min())
                    valid = (np.isfinite(tstar) & (tstar > 0)
                             & (tstar / fps < ttc_threshold)
                             & (dmin < conflict_dist))
                    ttc = np.where(valid, tstar / fps, np.inf)
                    j = int(np.argmin(ttc))
                    if not np.isfinite(ttc[j]):
                        C['ttc_no_sustained_closing'] += 1
                    else:
                        ha = _heading(pa, int(np.searchsorted(
                            fa, samples[j + 1]).clip(0, len(pa) - 1)))
                        hb = _heading(pb, int(np.searchsorted(
                            fb, samples[j + 1]).clip(0, len(pb) - 1)))
                        ang = None
                        if ha is not None and hb is not None:
                            ang = abs(ha - hb)
                            ang = 360 - ang if ang > 180 else ang
                        parallel = ang is not None and ang < min_angle
                        head_on = ang is not None and ang > 180 - 2 * min_angle
                        never_near = min_dact > margin
                        cand_dbg = {
                            'ttc': round(float(ttc[j]), 2),
                            'cpa_dist_px': round(float(dmin[j]), 1),
                            'dist_now_px': round(float(np.linalg.norm(r[j])), 1),
                            'min_dist_actual_px': round(min_dact, 1),
                            'rel_speed_pxs': round(float(np.sqrt(vv[j]) * fps), 1),
                            'conflict_dist_px': round(conflict_dist, 1),
                            'margin_px': round(margin, 1),
                            'angle': None if ang is None else round(ang, 1),
                            'heading_a': None if ha is None else round(ha, 1),
                            'heading_b': None if hb is None else round(hb, 1),
                            'frame': int(samples[j + 1]),
                            'diag_a': round(ta['diag'], 1),
                            'diag_b': round(tb['diag'], 1)}
                        if parallel:
                            C['ttc_parallel_veto'] += 1
                            dbg['ttc_vetoed_candidates'].append(
                                dict(cand_dbg, a_id=int(aid), b_id=int(bid),
                                     a_cls=ta['cls'], b_cls=tb['cls'],
                                     veto='parallel'))
                        elif head_on:
                            C['ttc_head_on_veto'] += 1
                            dbg['ttc_vetoed_candidates'].append(
                                dict(cand_dbg, a_id=int(aid), b_id=int(bid),
                                     a_cls=ta['cls'], b_cls=tb['cls'],
                                     veto='head_on'))
                        elif never_near:
                            C['ttc_never_near_veto'] += 1
                            dbg['ttc_vetoed_candidates'].append(
                                dict(cand_dbg, a_id=int(aid), b_id=int(bid),
                                     a_cls=ta['cls'], b_cls=tb['cls'],
                                     veto='never_near'))
                        else:
                            mid = (Ps_a[j + 1] + Ps_b[j + 1]) / 2
                            spd = _speed_at(pa, fa, int(np.searchsorted(
                                fa, samples[j + 1]).clip(0, len(pa) - 1)))
                            best = {'type': 'TTC', 'a_id': int(aid),
                                    'b_id': int(bid),
                                    'point': [float(mid[0]), float(mid[1])],
                                    'pet': None, 'ttc': round(float(ttc[j]), 2),
                                    'frame': int(samples[j + 1]),
                                    'angle': None if ang is None else round(ang, 1),
                                    'speed': round(spd, 2),
                                    'a_cls': ta['cls'], 'b_cls': tb['cls'],
                                    'severity': severity(None, spd, fps),
                                    'dbg': cand_dbg}
            if best is not None:
                C['pet_hits' if best['type'] == 'PET' else 'ttc_hits'] += 1
                events.append(best)

    order = {'critical': 0, 'severe': 1, 'moderate': 2, 'slight': 3}
    events.sort(key=lambda e: (order[e['severity']],
                               e['pet'] if e['pet'] is not None else e['ttc']))
    log.info(f"near-miss: {len(events)} conflicts "
             f"({len(vehs)} veh, {len(peds)} ped, mode={mode})")

    # ── write debug log (share this file to analyze any suspect conflict) ──
    if debug:
        try:
            import os
            dbg['events'] = events
            dbg['ttc_vetoed_candidates'].sort(key=lambda c: c['ttc'])
            dbg['ttc_vetoed_candidates'] = dbg['ttc_vetoed_candidates'][:50]
            base = os.path.dirname(os.path.abspath(traf_path))
            jpath = os.path.join(base, 'nearmiss_debug.json')
            with open(jpath, 'w', encoding='utf-8') as f:
                json.dump(dbg, f, indent=1)
            lpath = os.path.join(base, 'nearmiss_debug.log')
            with open(lpath, 'w', encoding='utf-8') as f:
                r = dbg['run']
                f.write(f"NEAR-MISS DEBUG  {r['timestamp']}  "
                        f"engine={r['engine']}  ({r['geometry']})\n")
                f.write(f"traf={r['traf']}\nmode={r['mode']}  fps={r['fps']}  "
                        f"pet<={r['pet_threshold']}  ttc<={r['ttc_threshold']}  "
                        f"min_angle={r['min_angle']}  diag_factor={r['diag_factor']}"
                        f"  min_diag={r['min_diag']}\n")
                f.write(f"tracks: total={r['tracks_total']} usable={r['tracks_usable']}"
                        f" stationary={r['dropped_stationary']}"
                        f" small={r['dropped_small_diag']}\n")
                f.write("counters: " + "  ".join(
                    f"{k}={v}" for k, v in dbg['counters'].items()) + "\n\n")
                f.write(f"== {len(events)} EVENTS ==\n")
                for e in events:
                    d = e.get('dbg', {})
                    f.write(f"[{e['severity'].upper():8s}] {e['type']} "
                            f"{e['a_cls']}#{e['a_id']} x {e['b_cls']}#{e['b_id']}"
                            f" @f{e['frame']}  pet={e['pet']} ttc={e['ttc']}"
                            f" angle={e.get('angle')}  " +
                            "  ".join(f"{k}={v}" for k, v in d.items()) + "\n")
                f.write(f"\n== TOP VETOED TTC CANDIDATES "
                        f"(would have fired in v1) ==\n")
                for c in dbg['ttc_vetoed_candidates']:
                    f.write(f"[veto={c['veto']:9s}] {c['a_cls']}#{c['a_id']} x "
                            f"{c['b_cls']}#{c['b_id']} @f{c['frame']}"
                            f"  ttc={c['ttc']} cpa_dist={c['cpa_dist_px']}"
                            f" dist_now={c['dist_now_px']}"
                            f" min_dist_actual={c.get('min_dist_actual_px')}"
                            f" rel_speed={c['rel_speed_pxs']}px/s"
                            f" angle={c['angle']} margin={c['margin_px']}\n")
            log.info(f"near-miss debug written: {jpath}")
        except Exception as e:
            log.warning(f"near-miss debug log failed: {e}")

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
