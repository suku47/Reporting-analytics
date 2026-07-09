"""
Convert legacy vehicle_tracks_all.pkl files to .traf format.

This replaces the older converter. Key differences:

  - Treats the pkl as a *sequence* of video segments (one per
    video_start_time entry) and stitches them into a single continuous
    .traf timeline. Each track is mapped to its source segment via
    timestamps[0], and frame indices become:
        global_frame = segment_offset + int((ts - seg_start) * fps)
  - Sequential integer track_ids (no hash collisions on 4498+ tracks).
  - Reads timestamps to compute real first_frame / last_frame / duration
    rather than treating every track as starting at frame 0.
  - Defaults fps to 5.0 (matches the legacy pkl sampling); override with
    --fps if your data is different.
  - US-scheme class labels (PV / SU / CU / MC / BUS) with sensible
    fallbacks for unmapped IDs; override with --class-map.
  - Logs conversion stats (kept / skipped, class distribution,
    per-segment track counts) so you can sanity-check the result.

Usage:
    python tools/convert_pkl_to_traf.py --pkl path/to/vehicle_tracks_all.pkl \
                                        --output path/to/converted.traf
    # Override fps if needed:
    python tools/convert_pkl_to_traf.py --pkl X.pkl --output Y.traf --fps 5
    # Override class mapping:
    python tools/convert_pkl_to_traf.py --pkl X.pkl --output Y.traf \
        --class-map "1:MC,2:PV,3:SU,4:CU,5:BUS,7:PV"
"""
import argparse
import json
import logging
import math
import os
import pickle
import sqlite3
import sys
from collections import Counter, defaultdict

import numpy as np


# --- US class scheme defaults --------------------------------------------------
# class_id -> short / full name. Falls through to 'UNK' for anything not listed;
# override at the CLI with --class-map.
DEFAULT_CLASS_SHORT = {
    1: 'MC',  2: 'PV',  3: 'SU',  4: 'CU',
    5: 'BUS', 6: 'UNK', 7: 'PV',
}
DEFAULT_CLASS_FULL = {
    'MC':  'Motorcycle',
    'PV':  'Passenger Vehicle',
    'SU':  'Single-Unit Truck',
    'CU':  'Combination-Unit Truck',
    'BUS': 'Bus',
    'UNK': 'Unknown',
}

# Inline schema — kept identical to schema.sql so a viewer built against
# either source will read the file the same way.
_DDL = """
CREATE TABLE IF NOT EXISTS scene (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tracks (
    track_id INTEGER PRIMARY KEY, class_id INTEGER, class_name TEXT,
    class_full_name TEXT, first_frame INTEGER, last_frame INTEGER,
    total_frames INTEGER, observed_frames INTEGER, duration_sec REAL,
    entry_x REAL, entry_y REAL, exit_x REAL, exit_y REAL,
    entry_edge TEXT, exit_edge TEXT, speed_mean_px REAL, speed_max_px REAL,
    speed_mean_world REAL, speed_max_world REAL, is_stationary INTEGER,
    stationary_duration_sec REAL, bbox_diag_mean REAL, bbox_diag_min REAL,
    bbox_diag_max REAL, num_tracklets INTEGER, tracklet_indices TEXT,
    track_quality REAL, trajectory_json TEXT);
CREATE TABLE IF NOT EXISTS observations (
    track_id INTEGER, frame INTEGER, cx REAL, cy REAL,
    bbox_x1 REAL, bbox_y1 REAL, bbox_x2 REAL, bbox_y2 REAL,
    timestamp TEXT, observed INTEGER, speed_px REAL,
    PRIMARY KEY (track_id, frame));
CREATE TABLE IF NOT EXISTS gates (
    gate_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    x1 REAL, y1 REAL, x2 REAL, y2 REAL, direction TEXT,
    approach TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS gate_crossings (
    crossing_id INTEGER PRIMARY KEY AUTOINCREMENT, gate_id INTEGER,
    track_id INTEGER, frame INTEGER, timestamp TEXT, direction TEXT,
    speed_px REAL, class_id INTEGER, class_name TEXT);
CREATE TABLE IF NOT EXISTS tracklets (
    tracklet_idx INTEGER PRIMARY KEY, local_id INTEGER,
    start_frame INTEGER, end_frame INTEGER, length INTEGER,
    class_id INTEGER, start_x REAL, start_y REAL, end_x REAL,
    end_y REAL, start_zone TEXT, end_zone TEXT, speed_mean REAL,
    bbox_diag REAL, is_stationary INTEGER, global_track_id INTEGER);
CREATE TABLE IF NOT EXISTS annotations (
    annotation_id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT,
    name TEXT, geometry_json TEXT, properties_json TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_obs_track ON observations(track_id);
CREATE INDEX IF NOT EXISTS idx_obs_frame ON observations(frame);
"""


def parse_class_map_arg(s):
    """Parse --class-map '1:MC,2:PV,3:SU' into {int: str}."""
    out = {}
    for pair in s.split(','):
        pair = pair.strip()
        if not pair:
            continue
        if ':' not in pair:
            raise argparse.ArgumentTypeError(
                f"bad --class-map entry {pair!r}, expected 'id:name'")
        k, v = pair.split(':', 1)
        out[int(k.strip())] = v.strip()
    return out


def build_segment_table(start_times, end_times, fps):
    """
    Compute the global frame offset for each video segment so that segments
    concatenate end-to-end on the viewer's timeline.

    Returns:
        offsets       (list[int]) cumulative start frame for each segment
        durations_f   (list[int]) per-segment length in frames
        total_frames  (int)
    """
    offsets = []
    durations_f = []
    cum = 0
    for s, e in zip(start_times, end_times):
        dur_sec = (e - s).total_seconds()
        if dur_sec < 0:
            dur_sec = 0
        frames = max(1, int(math.ceil(dur_sec * fps)))
        offsets.append(cum)
        durations_f.append(frames)
        cum += frames
    return offsets, durations_f, cum


def find_segment(ts, start_times, end_times):
    """Return the index of the video segment whose [start, end] contains ts,
    or None if no segment matches."""
    for i, (s, e) in enumerate(zip(start_times, end_times)):
        if s <= ts <= e:
            return i
    return None


def auto_detect_frame_size(vehicle_tracks):
    """Inspect all positions to guess the source video frame size.
    Returns (width, height) rounded to common video dimensions when close."""
    max_x = max_y = 0.0
    for t in vehicle_tracks.values():
        p = t.get('positions')
        if p is None or len(p) == 0:
            continue
        max_x = max(max_x, float(np.max(p[:, 0])))
        max_y = max(max_y, float(np.max(p[:, 1])))
    # Round up with a 5% margin, then snap to common sizes
    w = int(math.ceil(max_x * 1.05))
    h = int(math.ceil(max_y * 1.05))
    for std_w, std_h in [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)]:
        if w <= std_w and h <= std_h:
            return std_w, std_h
    return max(w, 1280), max(h, 720)


def convert(pkl_path, output_path, fps=5.0, frame_width=None, frame_height=None,
            class_map_override=None):
    logging.info(f"Loading pkl: {pkl_path}")
    with open(pkl_path, 'rb') as f:
        data = pickle.load(f)

    vehicle_tracks = data.get('vehicle_tracks', {})
    start_times = data.get('video_start_time', [])
    end_times = data.get('video_end_time', [])
    if len(start_times) != len(end_times):
        raise ValueError(
            f"video_start_time ({len(start_times)}) and video_end_time "
            f"({len(end_times)}) lengths do not match")

    logging.info(f"  segments: {len(start_times)}")
    logging.info(f"  raw tracks: {len(vehicle_tracks)}")

    # Resolve class maps
    short_map = dict(DEFAULT_CLASS_SHORT)
    if class_map_override:
        short_map.update(class_map_override)
    full_map = {cid: DEFAULT_CLASS_FULL.get(short, short)
                for cid, short in short_map.items()}

    # Frame size: auto-detect from positions if user didn't pass one
    if frame_width is None or frame_height is None:
        det_w, det_h = auto_detect_frame_size(vehicle_tracks)
        frame_width = frame_width or det_w
        frame_height = frame_height or det_h
        logging.info(f"  auto frame size: {frame_width}x{frame_height}")

    # Segment offsets
    offsets, durations_f, total_frames = build_segment_table(
        start_times, end_times, fps)
    logging.info(f"  total timeline: {total_frames} frames @ {fps} fps "
                 f"= {total_frames/fps:.0f}s")

    # DB setup
    if os.path.exists(output_path):
        os.remove(output_path)
    conn = sqlite3.connect(output_path)
    conn.executescript(_DDL)

    # Process tracks
    next_track_id = 0
    kept = 0
    skipped_short = 0
    skipped_no_segment = 0
    skipped_no_timestamps = 0
    class_counter = Counter()
    per_segment_kept = defaultdict(int)
    timeline_first_frame = float('inf')
    timeline_last_frame = -1

    insert_track_sql = "INSERT INTO tracks VALUES (" + ",".join(["?"] * 28) + ")"
    insert_obs_sql = "INSERT OR IGNORE INTO observations VALUES (" + ",".join(["?"] * 11) + ")"

    for original_id, tdata in vehicle_tracks.items():
        positions = np.asarray(tdata.get('positions', []), dtype=np.float64)
        timestamps = tdata.get('timestamps', [])

        if len(positions) < 2:
            skipped_short += 1
            continue
        if not timestamps or len(timestamps) != len(positions):
            skipped_no_timestamps += 1
            continue

        seg_idx = find_segment(timestamps[0], start_times, end_times)
        if seg_idx is None:
            skipped_no_segment += 1
            continue

        seg_start = start_times[seg_idx]
        seg_offset = offsets[seg_idx]
        seg_len_frames = durations_f[seg_idx]

        # Map every observation's timestamp -> global frame
        frames = []
        for ts in timestamps:
            local_f = int((ts - seg_start).total_seconds() * fps)
            # Clamp into segment to avoid spilling into the next segment's
            # range if a timestamp slightly overshoots seg_end
            if local_f < 0:
                local_f = 0
            elif local_f >= seg_len_frames:
                local_f = seg_len_frames - 1
            frames.append(seg_offset + local_f)

        # Dedupe — multiple samples within the same 1/fps slot become one
        seen = set()
        keep_idx = []
        for i, f in enumerate(frames):
            if f not in seen:
                seen.add(f)
                keep_idx.append(i)
        if len(keep_idx) < 2:
            skipped_short += 1
            continue

        frames_c = [frames[i] for i in keep_idx]
        pos_c = positions[keep_idx]
        ts_c = [timestamps[i] for i in keep_idx]

        next_track_id += 1
        gid = next_track_id
        class_id = int(tdata.get('class_id', 0))
        class_counter[class_id] += 1
        per_segment_kept[seg_idx] += 1
        cls_short = short_map.get(class_id, 'UNK')
        cls_full = full_map.get(class_id, DEFAULT_CLASS_FULL.get(cls_short, 'Unknown'))

        n = len(frames_c)
        first_frame = frames_c[0]
        last_frame = frames_c[-1]
        if first_frame < timeline_first_frame:
            timeline_first_frame = first_frame
        if last_frame > timeline_last_frame:
            timeline_last_frame = last_frame

        # Speed (px per frame, scaled by the per-step frame gap to handle
        # the rare dedupe case where consecutive frames are >1 apart)
        speeds = []
        for i in range(1, n):
            d = float(np.linalg.norm(pos_c[i] - pos_c[i - 1]))
            df = max(1, frames_c[i] - frames_c[i - 1])
            speeds.append(d / df)
        speed_mean = float(np.mean(speeds)) if speeds else 0.0
        speed_max = float(np.max(speeds)) if speeds else 0.0
        duration_sec = (ts_c[-1] - ts_c[0]).total_seconds()
        total_disp = float(np.linalg.norm(pos_c[-1] - pos_c[0]))
        is_stat = 1 if total_disp < 30.0 else 0

        # Sampled trajectory_json (kept compatible with viewer)
        step = max(1, n // 200)
        traj = [[round(float(pos_c[i][0]), 1),
                 round(float(pos_c[i][1]), 1),
                 frames_c[i]]
                for i in range(0, n, step)]
        if not traj or traj[-1][2] != frames_c[-1]:
            traj.append([round(float(pos_c[-1][0]), 1),
                         round(float(pos_c[-1][1]), 1),
                         frames_c[-1]])

        conn.execute(insert_track_sql, (
            gid, class_id, cls_short, cls_full,
            first_frame, last_frame, n, n, round(duration_sec, 2),
            float(pos_c[0][0]), float(pos_c[0][1]),
            float(pos_c[-1][0]), float(pos_c[-1][1]),
            'mid', 'mid', round(speed_mean, 2), round(speed_max, 2),
            None, None, is_stat, 0.0,
            None, None, None,        # bbox diag: pkl has no bboxes
            1, '[]', 0.8, json.dumps(traj),
        ))

        obs_rows = []
        for i, frm in enumerate(frames_c):
            spd = speeds[i - 1] if i > 0 else 0.0
            ts_iso = ts_c[i].isoformat() if hasattr(ts_c[i], 'isoformat') else str(ts_c[i])
            obs_rows.append((
                gid, frm,
                float(pos_c[i][0]), float(pos_c[i][1]),
                None, None, None, None,       # no bbox
                ts_iso, 1, round(spd, 2),
            ))
        conn.executemany(insert_obs_sql, obs_rows)

        kept += 1

    # Scene metadata
    earliest_ts = min(start_times) if start_times else None
    scene_meta = {
        'schema_version': '1.0.0',
        'fps': str(fps),
        'frame_width': str(frame_width),
        'frame_height': str(frame_height),
        'video_path': os.path.basename(pkl_path).replace('.pkl', ''),
        'has_calibration': '0',
        'total_vehicles': str(kept),
        'total_frames': str(total_frames),
        'class_map': json.dumps({str(k): v for k, v in short_map.items()}),
        'class_full_map': json.dumps({str(k): full_map[k] for k in short_map}),
    }
    if earliest_ts is not None:
        scene_meta['video_start_time'] = (
            earliest_ts.isoformat() if hasattr(earliest_ts, 'isoformat')
            else str(earliest_ts))
    conn.executemany("INSERT INTO scene VALUES (?,?)", list(scene_meta.items()))

    conn.commit()
    conn.close()

    # Reporting
    print()
    print(f"=== Conversion summary: {pkl_path} -> {output_path} ===")
    print(f"  Kept:                  {kept:>5} tracks")
    print(f"  Skipped (<2 points):   {skipped_short:>5}")
    print(f"  Skipped (no timestamps):{skipped_no_timestamps:>5}")
    print(f"  Skipped (no segment):  {skipped_no_segment:>5}")
    print()
    print(f"  Timeline span:         frames {timeline_first_frame}..{timeline_last_frame} "
          f"of {total_frames} ({fps} fps)")
    print()
    print(f"  Class distribution (US scheme, ID -> short_name):")
    for cid in sorted(class_counter):
        nm = short_map.get(cid, 'UNK')
        print(f"    {cid:>2} ({nm:<4}): {class_counter[cid]:>5} tracks")
    print()
    print(f"  Per-segment kept counts:")
    for seg_idx in range(len(start_times)):
        c = per_segment_kept.get(seg_idx, 0)
        s = start_times[seg_idx]
        e = end_times[seg_idx]
        offs = offsets[seg_idx]
        durf = durations_f[seg_idx]
        s_str = s.isoformat() if hasattr(s, 'isoformat') else str(s)
        print(f"    seg {seg_idx:>2}: {s_str}  frames {offs}..{offs+durf-1}  "
              f"({c} tracks)")
    print()


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pkl', required=True, help='Path to vehicle_tracks_all.pkl')
    p.add_argument('--output', required=True, help='Path to output .traf')
    p.add_argument('--fps', type=float, default=5.0,
                   help='Frame rate for the output timeline (default: 5.0, '
                        'matches the legacy pkl sampling). The pkl positions '
                        'are sampled at ~5 fps; using a higher fps will not '
                        'create more data, just spread it on a sparser grid.')
    p.add_argument('--frame-width', type=int, default=None,
                   help='Source video width in px (auto-detected if omitted)')
    p.add_argument('--frame-height', type=int, default=None,
                   help='Source video height in px (auto-detected if omitted)')
    p.add_argument('--class-map', type=parse_class_map_arg, default=None,
                   help='Override class_id -> short_name mapping, '
                        "e.g. '1:MC,2:PV,3:SU,4:CU,5:BUS,7:PV'")
    p.add_argument('-v', '--verbose', action='store_true')
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s')

    try:
        convert(args.pkl, args.output, fps=args.fps,
                frame_width=args.frame_width, frame_height=args.frame_height,
                class_map_override=args.class_map)
    except Exception as e:
        logging.exception(f"Conversion failed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
