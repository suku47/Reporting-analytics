"""
Convert legacy vehicle_tracks_all.pkl files to .traf format.

Usage:
  python tools/convert_pkl_to_traf.py --pkl results/vehicle_tracks_all.pkl --output results/converted.traf
"""
import sys, os, pickle, json, sqlite3
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


CLASS_ID_TO_NAME = {0: "PV", 1: "SU", 2: "CU", 3: "MC", 5: "BUS"}
CLASS_ID_TO_FULL = {0: "Passenger Vehicle", 1: "Single-Unit Truck",
                    2: "Combination-Unit Truck", 3: "Motorcycle", 5: "Bus"}


def convert(pkl_path, output_path, fps=30.0, frame_width=1280, frame_height=720):
    with open(pkl_path, 'rb') as f:
        data = pickle.load(f)

    vehicle_tracks = data.get('vehicle_tracks', {})
    start_times = data.get('video_start_time', [])

    if os.path.exists(output_path):
        os.remove(output_path)
    conn = sqlite3.connect(output_path)

    # Create schema
    conn.executescript(open(os.path.join(os.path.dirname(__file__), '..',
                      'schema.sql')).read() if os.path.exists('schema.sql') else """
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
    """)

    # Scene metadata
    meta = {
        'schema_version': '1.0.0', 'fps': str(fps),
        'frame_width': str(frame_width), 'frame_height': str(frame_height),
        'video_path': os.path.basename(pkl_path).replace('.pkl', ''),
        'has_calibration': '0',
        'total_vehicles': str(len(vehicle_tracks)),
        'class_map': json.dumps(CLASS_ID_TO_NAME),
    }
    conn.executemany("INSERT INTO scene VALUES (?,?)", meta.items())

    # Convert tracks
    max_frame = 0
    for gid_str, tdata in vehicle_tracks.items():
        gid = int(gid_str) if isinstance(gid_str, (int, float)) else hash(gid_str) % 100000
        positions = np.array(tdata['positions'])
        timestamps = tdata.get('timestamps', [])
        class_id = tdata.get('class_id', 0)
        cls_name = CLASS_ID_TO_NAME.get(class_id, 'UNK')

        if len(positions) < 2:
            continue

        n = len(positions)
        speeds = [np.linalg.norm(positions[i] - positions[i-1])
                  for i in range(1, n)]
        speed_mean = np.mean(speeds) if speeds else 0
        speed_max = np.max(speeds) if speeds else 0
        is_stat = 1 if np.linalg.norm(positions[-1] - positions[0]) < 30 else 0

        # Sample trajectory
        step = max(1, n // 200)
        traj = [[round(float(positions[i][0]), 1),
                 round(float(positions[i][1]), 1), i]
                for i in range(0, n, step)]

        first_frame = 0
        last_frame = n - 1
        max_frame = max(max_frame, last_frame)

        conn.execute("""INSERT OR IGNORE INTO tracks VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            gid, class_id, cls_name, CLASS_ID_TO_FULL.get(class_id, 'Unknown'),
            first_frame, last_frame, n, n, round(n / fps, 2),
            float(positions[0][0]), float(positions[0][1]),
            float(positions[-1][0]), float(positions[-1][1]),
            'mid', 'mid', round(speed_mean, 2), round(speed_max, 2),
            None, None, is_stat, 0.0, 100.0, 80.0, 120.0,
            1, '[]', 0.8, json.dumps(traj),
        ))

        # Observations
        obs = []
        for i in range(n):
            spd = speeds[i-1] if i > 0 else 0
            obs.append((gid, i, float(positions[i][0]), float(positions[i][1]),
                        None, None, None, None, None, 1, round(spd, 2)))
        conn.executemany("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)", obs)

    meta['total_frames'] = str(max_frame)
    conn.execute("INSERT OR REPLACE INTO scene VALUES ('total_frames', ?)", (str(max_frame),))
    conn.commit()
    conn.close()

    print(f"Converted {len(vehicle_tracks)} tracks → {output_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--pkl', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--fps', type=float, default=30.0)
    args = parser.parse_args()
    convert(args.pkl, args.output, fps=args.fps)
