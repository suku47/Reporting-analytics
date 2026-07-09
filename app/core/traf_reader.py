"""
TrafficReader — Read and query .traf SQLite databases.

Usage:
    from app.core.traf_reader import TrafficReader
    
    with TrafficReader("output.traf") as reader:
        print(reader.summary())
        tracks = reader.get_tracks(class_name="PV")
"""

import sqlite3
import json


class TrafficReader:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._meta = None

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def meta(self):
        if self._meta is None:
            self._meta = {}
            for row in self.conn.execute("SELECT key, value FROM scene"):
                self._meta[row['key']] = row['value']
        return self._meta

    @property
    def fps(self):
        return float(self.meta.get('fps', 30.0))

    @property
    def frame_size(self):
        return (int(self.meta.get('frame_width', 1280)),
                int(self.meta.get('frame_height', 720)))

    def summary(self):
        total = self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        stationary = self.conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE is_stationary=1").fetchone()[0]
        classes = {}
        for row in self.conn.execute(
                "SELECT class_name, COUNT(*) as cnt FROM tracks GROUP BY class_name"):
            classes[row['class_name']] = row['cnt']
        return {
            'total_tracks': total,
            'stationary_tracks': stationary,
            'moving_tracks': total - stationary,
            'class_counts': classes,
            'fps': self.fps,
            'frame_size': self.frame_size,
        }

    def get_tracks(self, **filters):
        sql = "SELECT * FROM tracks WHERE 1=1"
        params = []
        for key, val in filters.items():
            if val is not None:
                sql += f" AND {key}=?"
                params.append(val)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY track_id", params)]

    def get_observations(self, track_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM observations WHERE track_id=? ORDER BY frame", (track_id,))]

    def get_trajectory(self, track_id, sampled=True):
        if sampled:
            row = self.conn.execute(
                "SELECT trajectory_json FROM tracks WHERE track_id=?",
                (track_id,)).fetchone()
            if row and row['trajectory_json']:
                return json.loads(row['trajectory_json'])
        return [(r['cx'], r['cy'], r['frame'])
                for r in self.conn.execute(
                    "SELECT cx, cy, frame FROM observations "
                    "WHERE track_id=? ORDER BY frame", (track_id,))]
