"""
Movement Inference v2 — leg-based movement classification with learned
reference paths.

Designed for junctions (bi-directional, 3-leg, 4-leg) with ONE gate per
leg/approach. Movements (including U-turns) are the ordered pairs of leg
gates and can be auto-generated — no per-site configuration beyond
placing and naming the leg gates.

Self-calibrating: every signature is learned per site from that site's
own complete (direct) tracks. Display-only: nothing here writes to the
.traf; all classification happens at export/query time.

Pipeline
--------
1. DIRECT: a track's gate-crossing sequence (engine v3 records ALL
   crossings, debounced) is paired into movement segments:
   (c0,c1), (c2,c3), ... Each pair of different gates = a through/turn
   movement. A pair on the SAME gate = a U-turn candidate, accepted only
   if it passes validation (opposite crossing signs, minimum time gap,
   heading reversal) — otherwise flagged as suspected jitter.
2. REFERENCE PATHS: for each movement with >= MIN_CONFIRMED_FOR_REFERENCE
   direct tracks, the trajectory portions between entry and exit
   crossings are arc-length resampled and combined point-wise by median
   into one reference polyline (with direction). A per-movement corridor
   width is learned from how tightly the direct tracks hug their own
   reference — so acceptance thresholds adapt to each site.
3. SINGLE-CROSSING tracks (born late / lost early): candidates are
   movements consistent with the crossing's (gate, direction sign);
   each candidate is then shape-verified against its reference path:
   mean distance within corridor, local direction agreement, forward
   progression. Best valid candidate with a clear margin is assigned
   (Inferred). Sign-only assignment is used as fallback when no
   reference exists yet.
4. ZERO-CROSSING tracks (died entirely between gates): matched purely
   by reference-path shape with stricter requirements (direction
   agreement, minimum path coverage). Counted as Inferred when they
   clearly belong to exactly one movement; otherwise unresolved.

Everything stays auditable: Direct vs Inferred separation, per-movement
report with reference quality, and explicit reasons for every
unresolved track.
"""

import json
import math
from collections import defaultdict, Counter

# ── Tunables (learned signatures do the heavy lifting; these are guards) ──
MIN_CONFIRMED_FOR_REFERENCE = 3   # direct tracks needed before a movement can attract inferred tracks
RESAMPLE_N = 50                   # points per resampled reference path
SIGN_DOMINANCE = 0.8              # fraction of direct tracks that must agree on a crossing sign
MIN_HEADING_DISPLACEMENT = 25.0   # px; below this a track's own direction is not trusted
DIR_AGREE_THRESHOLD = 0.70        # mean tangent agreement to accept a shape match
DIR_AGREE_ZERO_CROSS = 0.80       # stricter agreement for zero-crossing tracks
PROGRESSION_THRESHOLD = 0.70      # fraction of steps that must move forward along the path
SCORE_MARGIN = 0.15               # best candidate must beat runner-up by this margin
CORRIDOR_FLOOR_PX = 15.0          # minimum corridor half-width
CORRIDOR_P90_MULT = 1.5           # corridor = p90(direct mean distances) * this
MIN_COVERAGE_ZERO_CROSS = 0.15    # zero-crossing track must span this fraction of the path
MIN_MATCH_LEN_PX = 40.0           # min overlap length (px along the path) for any shape match
COUNT_ZERO_CROSSING = True        # count clear zero-crossing matches (always flagged Inferred)
UTURN_MIN_GAP_SEC = 2.0           # min time between the two crossings of a U-turn
UTURN_HEADING_DOT_MAX = -0.3      # entry vs exit heading must oppose (cos < this)


# ════════════════════════════════════════════════════════════════
# Geometry helpers
# ════════════════════════════════════════════════════════════════

def _unit(dx, dy):
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return None
    return (dx / n, dy / n)


def _resample(pts, n=RESAMPLE_N):
    """Arc-length resample a polyline [(x,y),...] to exactly n points."""
    if len(pts) < 2:
        return None
    seg_len = []
    total = 0.0
    for i in range(1, len(pts)):
        d = math.hypot(pts[i][0] - pts[i-1][0], pts[i][1] - pts[i-1][1])
        seg_len.append(d)
        total += d
    if total < 1e-6:
        return None
    out = [pts[0]]
    step = total / (n - 1)
    target = step
    acc = 0.0
    i = 0
    while len(out) < n - 1 and i < len(seg_len):
        if acc + seg_len[i] >= target - 1e-9:
            t = (target - acc) / seg_len[i] if seg_len[i] > 1e-9 else 0.0
            x = pts[i][0] + (pts[i+1][0] - pts[i][0]) * t
            y = pts[i][1] + (pts[i+1][1] - pts[i][1]) * t
            out.append((x, y))
            target += step
        else:
            acc += seg_len[i]
            i += 1
    while len(out) < n:
        out.append(pts[-1])
    return out


def _project_point(px, py, ref, cum):
    """
    Project point onto reference polyline.
    Returns (distance, arc_position s in [0,1]).
    cum = precomputed cumulative arc lengths (len == len(ref)).
    """
    best_d2 = float('inf')
    best_s = 0.0
    total = cum[-1] if cum[-1] > 1e-9 else 1.0
    for i in range(len(ref) - 1):
        ax, ay = ref[i]
        bx, by = ref[i + 1]
        vx, vy = bx - ax, by - ay
        L2 = vx * vx + vy * vy
        if L2 < 1e-12:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
        qx, qy = ax + vx * t, ay + vy * t
        d2 = (px - qx) ** 2 + (py - qy) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best_s = (cum[i] + math.sqrt(L2) * t) / total
    return math.sqrt(best_d2), best_s


def _cum_lengths(ref):
    cum = [0.0]
    for i in range(1, len(ref)):
        cum.append(cum[-1] + math.hypot(ref[i][0] - ref[i-1][0],
                                        ref[i][1] - ref[i-1][1]))
    return cum


def _ref_tangent_at(ref, s):
    """Unit tangent of reference path at arc position s in [0,1]."""
    idx = min(len(ref) - 2, max(0, int(s * (len(ref) - 1))))
    return _unit(ref[idx + 1][0] - ref[idx][0], ref[idx + 1][1] - ref[idx][1])


def _match_track_to_ref(pts, ref, cum):
    """
    Score a track's points against one reference path.
    Points that project beyond the reference ends (s clamped to 0 or 1)
    are excluded — the reference only spans gate-to-gate, while a track
    may legitimately extend before/after it.
    Returns dict(mean_dist, dir_agree, progression, coverage) or None.
    """
    if len(pts) < 2 or ref is None:
        return None
    EPS = 0.005
    proj = []
    for (x, y) in pts:
        d, s = _project_point(x, y, ref, cum)
        proj.append((d, s))
    interior = [i for i, (d, s) in enumerate(proj) if EPS < s < 1.0 - EPS]
    if len(interior) < 3:
        return None
    ss_all = [proj[i][1] for i in interior]
    total_len = cum[-1] if cum[-1] > 1e-9 else 1.0
    if (max(ss_all) - min(ss_all)) * total_len < MIN_MATCH_LEN_PX:
        return None
    dists = [proj[i][0] for i in interior]
    ss = ss_all
    # Direction agreement: track segment tangents vs reference tangents
    agrees = []
    for j in range(1, len(interior)):
        i0, i1 = interior[j - 1], interior[j]
        tu = _unit(pts[i1][0] - pts[i0][0], pts[i1][1] - pts[i0][1])
        if tu is None:
            continue
        ru = _ref_tangent_at(ref, (ss[j] + ss[j - 1]) / 2.0)
        if ru is None:
            continue
        agrees.append(tu[0] * ru[0] + tu[1] * ru[1])
    if not agrees:
        return None
    fwd = sum(1 for j in range(1, len(ss)) if ss[j] >= ss[j - 1] - 0.01)
    progression = fwd / max(1, len(ss) - 1)
    return {
        'mean_dist': sum(dists) / len(dists),
        'dir_agree': sum(agrees) / len(agrees),
        'progression': progression,
        'coverage': max(ss) - min(ss),
    }


# ════════════════════════════════════════════════════════════════
# Main entry point
# ════════════════════════════════════════════════════════════════

def get_movement_assignments(conn, movements, infer=True):
    """
    Compute per-movement track assignments with Direct/Inferred separation.

    Args:
        conn: sqlite3 connection to a .traf (gate_crossings populated by
              gate engine v3 — multiple crossings per gate per track)
        movements: list of dicts [{from_id, to_id, from_name, to_name}]
                   (may include from_id == to_id for U-turns)
        infer: when False, only direct assignments are returned

    Returns dict with keys:
        assignments: {(from_id, to_id): [{track_id, class_name,
                      entry_frame, exit_frame, inferred (0/1), basis}]}
        unresolved:  [{track_id, class_name, ..., gates_crossed, reason}]
        report:      per-movement counts + learned signature/reference
                     quality + parameters (the multi-site QA trail)
    """
    gate_names = {g[0]: g[1] for g in conn.execute("SELECT gate_id, name FROM gates")}
    try:
        fps = float(dict(conn.execute(
            "SELECT key, value FROM scene WHERE key='fps'")).get('fps', 30.0) or 30.0)
    except Exception:
        fps = 30.0

    def mov_label(key):
        f, t = gate_names.get(key[0], key[0]), gate_names.get(key[1], key[1])
        return f"{f} -> {t}" + (" (U-turn)" if key[0] == key[1] else "")

    # ── Load crossings per track, ordered by frame ──
    crossings_by_track = defaultdict(list)
    for r in conn.execute(
            "SELECT track_id, gate_id, frame, direction FROM gate_crossings ORDER BY frame"):
        crossings_by_track[r[0]].append(
            {'gate_id': r[1], 'frame': r[2], 'direction': r[3]})

    # ── Load moving-track metadata + trajectories ──
    track_meta = {}
    for r in conn.execute(
            "SELECT track_id, class_name, entry_x, entry_y, exit_x, exit_y, "
            "first_frame, last_frame, total_frames, speed_mean_px, "
            "entry_edge, exit_edge, trajectory_json "
            "FROM tracks WHERE is_stationary=0"):
        pts = []
        if r[12]:
            try:
                pts = [(p[0], p[1], p[2] if len(p) > 2 else i)
                       for i, p in enumerate(json.loads(r[12]))]
            except Exception:
                pts = []
        track_meta[r[0]] = {
            'class_name': r[1],
            'entry_x': r[2] or 0.0, 'entry_y': r[3] or 0.0,
            'exit_x': r[4] or 0.0, 'exit_y': r[5] or 0.0,
            'first_frame': r[6], 'last_frame': r[7],
            'total_frames': r[8] or 0, 'speed_mean_px': r[9] or 0.0,
            'entry_edge': r[10] or '', 'exit_edge': r[11] or '',
            'traj': pts,   # [(x, y, frame)]
        }

    def _traj_xy(tid, f_start=None, f_end=None):
        """Trajectory points (x,y), optionally clipped to a frame window."""
        pts = track_meta[tid]['traj']
        if f_start is None and f_end is None:
            return [(p[0], p[1]) for p in pts]
        out = [(p[0], p[1]) for p in pts
               if (f_start is None or p[2] >= f_start)
               and (f_end is None or p[2] <= f_end)]
        return out if len(out) >= 2 else [(p[0], p[1]) for p in pts]

    def _heading_window(tid, tail=False, k=5):
        """Unit heading over the first (or last) k trajectory points."""
        pts = track_meta[tid]['traj']
        if len(pts) < 2:
            return None
        seg = pts[-k:] if tail else pts[:k]
        if len(seg) < 2:
            seg = pts[-2:] if tail else pts[:2]
        return _unit(seg[-1][0] - seg[0][0], seg[-1][1] - seg[0][1])

    def _unresolved_entry(tid, reason, gates_crossed=''):
        tm = track_meta[tid]
        return {
            'track_id': tid, 'class_name': tm['class_name'],
            'entry_edge': tm['entry_edge'], 'exit_edge': tm['exit_edge'],
            'speed': round(tm['speed_mean_px'], 1),
            'total_frames': tm['total_frames'],
            'first_frame': tm['first_frame'], 'last_frame': tm['last_frame'],
            'gates_crossed': gates_crossed, 'reason': reason,
        }

    def _seq_label(crs):
        return ' -> '.join(
            f"{gate_names.get(c['gate_id'], c['gate_id'])}(f{c['frame']})" for c in crs)

    keys = [(m['from_id'], m['to_id']) for m in movements]
    key_set = set(keys)
    assignments = {k: [] for k in keys}
    unresolved = []

    # ════════════════════════════════════════
    # 1. DIRECT: pair the crossing sequence into movement segments
    # ════════════════════════════════════════
    single_crossing = []   # (tid, crossing)
    zero_crossing = []     # tid
    direct_members = defaultdict(list)   # key -> [(tid, entry_cross, exit_cross)]

    for tid, tm in track_meta.items():
        crs = crossings_by_track.get(tid, [])
        if not crs:
            zero_crossing.append(tid)
            continue
        if len(crs) == 1:
            single_crossing.append((tid, crs[0]))
            continue

        # Pair consecutive crossings: (c0,c1), (c2,c3), ...
        i = 0
        assigned_any = False
        while i + 1 < len(crs):
            c_in, c_out = crs[i], crs[i + 1]
            key = (c_in['gate_id'], c_out['gate_id'])
            if c_in['gate_id'] != c_out['gate_id']:
                if key in key_set:
                    assignments[key].append({
                        'track_id': tid, 'class_name': tm['class_name'],
                        'entry_frame': c_in['frame'], 'exit_frame': c_out['frame'],
                        'inferred': 0, 'basis': 'direct',
                    })
                    direct_members[key].append((tid, c_in, c_out))
                    assigned_any = True
                else:
                    unresolved.append(_unresolved_entry(
                        tid, f'Crossed undefined movement pair {mov_label(key)}',
                        gates_crossed=_seq_label(crs)))
            else:
                # Same-gate pair -> U-turn validation
                ok_gap = (c_out['frame'] - c_in['frame']) >= UTURN_MIN_GAP_SEC * fps
                ok_sign = c_in['direction'] != c_out['direction']
                h_in = _heading_window(tid, tail=False)
                h_out = _heading_window(tid, tail=True)
                ok_rev = (h_in is not None and h_out is not None and
                          (h_in[0] * h_out[0] + h_in[1] * h_out[1]) <= UTURN_HEADING_DOT_MAX)
                if ok_gap and ok_sign and ok_rev and key in key_set:
                    assignments[key].append({
                        'track_id': tid, 'class_name': tm['class_name'],
                        'entry_frame': c_in['frame'], 'exit_frame': c_out['frame'],
                        'inferred': 0, 'basis': 'direct (U-turn)',
                    })
                    direct_members[key].append((tid, c_in, c_out))
                    assigned_any = True
                else:
                    why = []
                    if not ok_sign: why.append('same sign')
                    if not ok_gap: why.append('gap too short')
                    if not ok_rev: why.append('no heading reversal')
                    unresolved.append(_unresolved_entry(
                        tid,
                        'Same-gate double crossing at '
                        f"{gate_names.get(c_in['gate_id'])} failed U-turn validation "
                        f"({', '.join(why) or 'movement not defined'}) — possible jitter",
                        gates_crossed=_seq_label(crs)))
            i += 2
        if i < len(crs) and not assigned_any:
            # odd leftover crossing with nothing assigned -> treat as single
            single_crossing.append((tid, crs[i]))

    direct_ids = set()
    for rows in assignments.values():
        for a in rows:
            direct_ids.add(a['track_id'])

    # ════════════════════════════════════════
    # 2. Learn signatures + reference paths from direct tracks
    # ════════════════════════════════════════
    signatures = {}
    references = {}   # key -> {'ref': [(x,y)...], 'cum': [...], 'corridor': px, 'n': int}

    for key in keys:
        members = direct_members.get(key, [])
        from_signs, to_signs = Counter(), Counter()
        resampled = []
        for tid, c_in, c_out in members:
            from_signs[c_in['direction']] += 1
            to_signs[c_out['direction']] += 1
            seg = _traj_xy(tid, c_in['frame'], c_out['frame'])
            rs = _resample(seg)
            if rs:
                resampled.append(rs)

        sig = {'n': len(members), 'from_sign': None, 'to_sign': None,
               'from_sign_agree': 0, 'to_sign_agree': 0}
        if len(members) >= MIN_CONFIRMED_FOR_REFERENCE:
            s, c = from_signs.most_common(1)[0]
            if c / len(members) >= SIGN_DOMINANCE:
                sig['from_sign'], sig['from_sign_agree'] = s, c
            s, c = to_signs.most_common(1)[0]
            if c / len(members) >= SIGN_DOMINANCE:
                sig['to_sign'], sig['to_sign_agree'] = s, c
        signatures[key] = sig

        if len(resampled) >= MIN_CONFIRMED_FOR_REFERENCE:
            ref = []
            for j in range(RESAMPLE_N):
                xs = sorted(r[j][0] for r in resampled)
                ys = sorted(r[j][1] for r in resampled)
                mid = len(resampled) // 2
                if len(resampled) % 2:
                    ref.append((xs[mid], ys[mid]))
                else:
                    ref.append(((xs[mid-1] + xs[mid]) / 2, (ys[mid-1] + ys[mid]) / 2))
            cum = _cum_lengths(ref)
            # Corridor: how tightly do the direct tracks hug their own reference
            mean_dists = []
            for rs in resampled:
                ds = [_project_point(x, y, ref, cum)[0] for (x, y) in rs]
                mean_dists.append(sum(ds) / len(ds))
            mean_dists.sort()
            p90 = mean_dists[min(len(mean_dists) - 1, int(0.9 * len(mean_dists)))]
            corridor = max(CORRIDOR_FLOOR_PX, p90 * CORRIDOR_P90_MULT)
            references[key] = {'ref': ref, 'cum': cum,
                               'corridor': corridor, 'n': len(resampled)}

    def _shape_candidates(tid, candidate_keys, strict=False):
        """
        Score track tid against each candidate's reference path.
        Returns (scored_valid, evaluable_count): evaluable_count is how
        many candidates produced a measurable match at all — zero means
        the track is too short / outside every reference's extent, which
        is different from positively lying off-path.
        """
        pts = _traj_xy(tid)
        scored = []
        evaluable = 0
        for key in candidate_keys:
            R = references.get(key)
            if not R:
                continue
            m = _match_track_to_ref(pts, R['ref'], R['cum'])
            if not m:
                continue
            evaluable += 1
            dir_min = DIR_AGREE_ZERO_CROSS if strict else DIR_AGREE_THRESHOLD
            if (m['mean_dist'] <= R['corridor']
                    and m['dir_agree'] >= dir_min
                    and m['progression'] >= PROGRESSION_THRESHOLD
                    and (not strict or m['coverage'] >= MIN_COVERAGE_ZERO_CROSS)):
                score = m['dir_agree'] * (1.0 - m['mean_dist'] / (2 * R['corridor']))
                scored.append((score, key, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored, evaluable

    # ════════════════════════════════════════
    # 3. SINGLE-CROSSING tracks: sign filter + shape verification
    # ════════════════════════════════════════
    inferred_zero = 0
    if infer:
        for tid, c in single_crossing:
            if tid in direct_ids:
                continue
            g, d = c['gate_id'], c['direction']
            g_name = gate_names.get(g, g)
            gates_lbl = f'{g_name}(f{c["frame"]})'

            # Candidates consistent with (gate, sign)
            cands = set()
            for key, sig in signatures.items():
                if sig['n'] < MIN_CONFIRMED_FOR_REFERENCE:
                    continue
                if key[0] == g and sig['from_sign'] == d:
                    cands.add(key)
                if key[1] == g and sig['to_sign'] == d:
                    cands.add(key)
            if not cands:
                unresolved.append(_unresolved_entry(
                    tid, f'Crossed only {g_name} ({d}); no movement signature matches',
                    gates_crossed=gates_lbl))
                continue

            with_ref = [k for k in cands if k in references]
            if with_ref:
                scored, evaluable = _shape_candidates(tid, with_ref)
                if scored and (len(scored) == 1 or
                               scored[0][0] - scored[1][0] >= SCORE_MARGIN):
                    score, key, m = scored[0]
                    assignments[key].append({
                        'track_id': tid,
                        'class_name': track_meta[tid]['class_name'],
                        'entry_frame': c['frame'], 'exit_frame': None,
                        'inferred': 1,
                        'basis': f"path d={m['mean_dist']:.0f}px dir={m['dir_agree']:.2f}",
                    })
                    continue
                if scored:
                    names = ' / '.join(mov_label(k) for _, k, _ in scored[:3])
                    unresolved.append(_unresolved_entry(
                        tid, f'Ambiguous between {names}; shape match inconclusive',
                        gates_crossed=gates_lbl))
                    continue
                if evaluable == 0 and len(cands) == 1:
                    # Track too short to shape-verify, but the sign is unambiguous
                    key = next(iter(cands))
                    assignments[key].append({
                        'track_id': tid,
                        'class_name': track_meta[tid]['class_name'],
                        'entry_frame': c['frame'], 'exit_frame': None,
                        'inferred': 1, 'basis': 'sign (too short for path match)',
                    })
                    continue
                names = ' / '.join(mov_label(k) for k in sorted(cands))
                if evaluable == 0:
                    unresolved.append(_unresolved_entry(
                        tid, f'Ambiguous between {names}; too short for shape match',
                        gates_crossed=gates_lbl))
                else:
                    unresolved.append(_unresolved_entry(
                        tid, f'Sign matches {names} but trajectory off reference path',
                        gates_crossed=gates_lbl))
                continue

            # No reference yet (low-traffic movement): sign-only fallback
            if len(cands) == 1:
                key = next(iter(cands))
                assignments[key].append({
                    'track_id': tid,
                    'class_name': track_meta[tid]['class_name'],
                    'entry_frame': c['frame'], 'exit_frame': None,
                    'inferred': 1, 'basis': 'sign',
                })
            else:
                names = ' / '.join(mov_label(k) for k in sorted(cands))
                unresolved.append(_unresolved_entry(
                    tid, f'Ambiguous between {names}; no reference paths to verify',
                    gates_crossed=gates_lbl))

        # ════════════════════════════════════════
        # 4. ZERO-CROSSING tracks: pure shape matching, strict
        # ════════════════════════════════════════
        for tid in zero_crossing:
            tm = track_meta[tid]
            disp = math.hypot(tm['exit_x'] - tm['entry_x'],
                              tm['exit_y'] - tm['entry_y'])
            if disp < MIN_HEADING_DISPLACEMENT:
                unresolved.append(_unresolved_entry(
                    tid, 'No gate crossing; displacement too small to classify'))
                continue
            scored, _evaluable = _shape_candidates(tid, list(references.keys()), strict=True)
            if COUNT_ZERO_CROSSING and scored and (
                    len(scored) == 1 or scored[0][0] - scored[1][0] >= SCORE_MARGIN):
                score, key, m = scored[0]
                mid_frame = (tm['first_frame'] + tm['last_frame']) // 2
                assignments[key].append({
                    'track_id': tid, 'class_name': tm['class_name'],
                    'entry_frame': mid_frame, 'exit_frame': None,
                    'inferred': 1,
                    'basis': (f"path (no crossing) d={m['mean_dist']:.0f}px "
                              f"dir={m['dir_agree']:.2f} cov={m['coverage']:.2f}"),
                })
                inferred_zero += 1
            elif scored:
                names = ' / '.join(mov_label(k) for _, k, _ in scored[:3])
                unresolved.append(_unresolved_entry(
                    tid, f'No gate crossing; shape ambiguous between {names}'))
            else:
                unresolved.append(_unresolved_entry(
                    tid, 'No gate crossing; does not match any reference path'))
    else:
        for tid in zero_crossing:
            unresolved.append(_unresolved_entry(tid, 'No gate crossing'))
        for tid, c in single_crossing:
            g_name = gate_names.get(c['gate_id'], c['gate_id'])
            unresolved.append(_unresolved_entry(
                tid, f'Only crossed 1 gate: {g_name} (frame {c["frame"]})',
                gates_crossed=f'{g_name}(f{c["frame"]})'))

    unresolved.sort(key=lambda x: x['track_id'])

    # ════════════════════════════════════════
    # 5. REPORT (QA trail for batch / multi-site runs)
    # ════════════════════════════════════════
    per_movement = []
    for key in keys:
        rows = assignments[key]
        sig = signatures[key]
        R = references.get(key)
        direct_n = sum(1 for a in rows if not a['inferred'])
        inferred_n = sum(1 for a in rows if a['inferred'])
        per_movement.append({
            'key': key,
            'label': mov_label(key),
            'is_uturn': key[0] == key[1],
            'direct': direct_n,
            'inferred': inferred_n,
            'total': direct_n + inferred_n,
            'from_sign': sig['from_sign'],
            'from_sign_agree': f"{sig['from_sign_agree']}/{sig['n']}" if sig['from_sign'] else '-',
            'to_sign': sig['to_sign'],
            'to_sign_agree': f"{sig['to_sign_agree']}/{sig['n']}" if sig['to_sign'] else '-',
            'ref_tracks': R['n'] if R else 0,
            'corridor_px': round(R['corridor'], 1) if R else None,
            'signature_trusted': sig['n'] >= MIN_CONFIRMED_FOR_REFERENCE,
        })

    report = {
        'per_movement': per_movement,
        'total_direct': sum(m['direct'] for m in per_movement),
        'total_inferred': sum(m['inferred'] for m in per_movement),
        'inferred_zero_crossing': inferred_zero,
        'unresolved_count': len(unresolved),
        'suggestions': [],   # superseded: zero-crossing matches are now counted
        'params': {
            'min_confirmed_for_reference': MIN_CONFIRMED_FOR_REFERENCE,
            'sign_dominance': SIGN_DOMINANCE,
            'dir_agree_threshold': DIR_AGREE_THRESHOLD,
            'progression_threshold': PROGRESSION_THRESHOLD,
            'score_margin': SCORE_MARGIN,
            'corridor_floor_px': CORRIDOR_FLOOR_PX,
            'corridor_p90_mult': CORRIDOR_P90_MULT,
            'count_zero_crossing': COUNT_ZERO_CROSSING,
            'uturn_min_gap_sec': UTURN_MIN_GAP_SEC,
        },
    }

    return {'assignments': assignments, 'unresolved': unresolved,
            'report': report}


def auto_generate_movements(conn, include_uturns=True):
    """
    Generate all ordered leg-gate pairs as movements.
    For n leg gates: n*(n-1) through/turn movements, plus n U-turns
    when include_uturns is True (n^2 total).
    """
    gates = conn.execute("SELECT gate_id, name FROM gates ORDER BY gate_id").fetchall()
    movs = []
    for g1 in gates:
        for g2 in gates:
            if g1[0] == g2[0] and not include_uturns:
                continue
            movs.append({'from_id': g1[0], 'to_id': g2[0],
                         'from_name': g1[1], 'to_name': g2[1]})
    return movs
