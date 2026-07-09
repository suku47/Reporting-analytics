"""
merge_trafs.py — Combine all of a site's .traf files into ONE site .traf.

Track IDs renumber sequentially across files (file 1 → 1..400, file 2
starts at 401, ...), and all frames are shifted onto a single real-time
timeline derived from each file's video_start_time — so a vehicle at
08:20:00 in the second recording lands exactly where the site clock says,
including across recording gaps.

The merged file behaves like any .traf: load it in the viewer, draw gates
once, and counts / trajectories / time filters operate on the whole site.

Usage:
    python merge_trafs.py --traf-dir "E:\\site\\Results\\traf" --out "E:\\site\\Results\\Site4_merged.traf"
"""

import argparse
import glob
import os
import sqlite3
import sys
from datetime import datetime


def merge(traf_dir, out_path, log=print):
    files = sorted(glob.glob(os.path.join(traf_dir, '*.traf')))
    if len(files) < 1:
        raise RuntimeError(f"no .traf files in {traf_dir}")

    # ── Pass 1: read metadata, order by real start time, validate ──
    metas = []
    for p in files:
        c = sqlite3.connect(p)
        m = dict(c.execute("SELECT key, value FROM scene"))
        c.close()
        try:
            start = datetime.fromisoformat(m['video_start_time'])
        except (KeyError, ValueError):
            raise RuntimeError(f"{os.path.basename(p)} has no video_start_time "
                               f"— required to build the site timeline")
        metas.append({'path': p, 'start': start,
                      'fps': float(m.get('fps', 30.0)),
                      'total_frames': int(m.get('total_frames', 0)),
                      'w': int(m.get('frame_width', 0)),
                      'h': int(m.get('frame_height', 0)),
                      'meta': m})
    metas.sort(key=lambda x: x['start'])

    fps0, w0, h0 = metas[0]['fps'], metas[0]['w'], metas[0]['h']
    for m in metas[1:]:
        if abs(m['fps'] - fps0) > 0.01:
            log(f"WARNING: {os.path.basename(m['path'])} fps {m['fps']} != "
                f"{fps0} — frame offsets use each file's own fps, timeline "
                f"stays correct, but per-frame stepping is approximate.")
        if (m['w'], m['h']) != (w0, h0):
            raise RuntimeError(f"{os.path.basename(m['path'])} frame size "
                               f"{m['w']}x{m['h']} differs from {w0}x{h0} — "
                               f"cannot merge different camera setups")

    site_start = metas[0]['start']
    site_end = max(m['start'].timestamp() + m['total_frames'] / m['fps']
                   for m in metas)
    site_total_frames = int((site_end - site_start.timestamp()) * fps0)

    if os.path.exists(out_path):
        os.remove(out_path)
    out = sqlite3.connect(out_path)

    # ── Schema: copy from first file (tables + indexes), plus provenance ──
    src0 = sqlite3.connect(metas[0]['path'])
    for (sql,) in src0.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"):
        out.execute(sql)
    src0.close()
    out.execute("""CREATE TABLE IF NOT EXISTS source_files (
        file TEXT, id_offset INTEGER, frame_offset INTEGER,
        video_start TEXT, n_tracks INTEGER)""")
    out.execute("CREATE TABLE IF NOT EXISTS assets (key TEXT PRIMARY KEY, data BLOB)")
    out.commit()

    # ── Pass 2: copy rows with id + frame offsets ──
    id_offset = 0
    background_stored = False
    for m in metas:
        src = sqlite3.connect(m['path'])
        src.row_factory = sqlite3.Row
        frame_offset = int((m['start'] - site_start).total_seconds() * m['fps'])

        n_tracks = 0
        max_tid = 0
        # tracks: shift track_id, first/last frame
        cols = [r[1] for r in src.execute("PRAGMA table_info(tracks)")]
        for row in src.execute("SELECT * FROM tracks"):
            d = dict(row)
            max_tid = max(max_tid, d['track_id'])
            d['track_id'] += id_offset
            for f in ('first_frame', 'last_frame'):
                if d.get(f) is not None:
                    d[f] += frame_offset
            # shift frame indices inside trajectory_json [[x,y,frame],...]
            if d.get('trajectory_json'):
                import json
                try:
                    tj = json.loads(d['trajectory_json'])
                    for pt in tj:
                        if len(pt) >= 3:
                            pt[2] += frame_offset
                    d['trajectory_json'] = json.dumps(tj)
                except Exception:
                    pass
            out.execute(f"INSERT INTO tracks ({','.join(cols)}) VALUES "
                        f"({','.join('?' * len(cols))})",
                        [d[c] for c in cols])
            n_tracks += 1

        # observations: shift track_id + frame
        ocols = [r[1] for r in src.execute("PRAGMA table_info(observations)")]
        ti, fi = ocols.index('track_id'), ocols.index('frame')
        buf = []
        for row in src.execute("SELECT * FROM observations"):
            vals = list(row)
            vals[ti] += id_offset
            vals[fi] += frame_offset
            buf.append(vals)
            if len(buf) >= 20000:
                out.executemany(f"INSERT INTO observations VALUES "
                                f"({','.join('?' * len(ocols))})", buf)
                buf = []
        if buf:
            out.executemany(f"INSERT INTO observations VALUES "
                            f"({','.join('?' * len(ocols))})", buf)

        # Other data tables (tracklets, annotations, anything future):
        # copy generically with id/frame offsets applied by column name.
        handled = {'scene', 'tracks', 'observations', 'gates', 'gate_crossings',
                   'assets', 'source_files', 'sqlite_sequence'}
        for (tname,) in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"):
            if tname in handled:
                continue
            tcols_info = list(src.execute(f"PRAGMA table_info({tname})"))
            tcols = [r[1] for r in tcols_info]
            # single-column INTEGER PK named like an id → let it autoincrement
            pk_cols = [r[1] for r in tcols_info if r[5]]
            drop_pk = (len(pk_cols) == 1 and
                       any(k in pk_cols[0].lower() for k in ('idx', 'id')) and
                       pk_cols[0] not in ('track_id',))
            use_cols = [c for c in tcols if not (drop_pk and c == pk_cols[0])]
            idx_of = {c: use_cols.index(c) for c in use_cols}
            rows_copied = 0
            for row in src.execute(f"SELECT {','.join(use_cols)} FROM {tname}"):
                vals = list(row)
                for c in use_cols:
                    if vals[idx_of[c]] is None:
                        continue
                    if c in ('track_id', 'global_track_id', 'local_id'):
                        vals[idx_of[c]] += id_offset
                    elif c in ('frame', 'start_frame', 'end_frame', 'first_frame',
                               'last_frame'):
                        vals[idx_of[c]] += frame_offset
                out.execute(f"INSERT INTO {tname} ({','.join(use_cols)}) "
                            f"VALUES ({','.join('?' * len(use_cols))})", vals)
                rows_copied += 1
            if rows_copied:
                log(f"      {tname}: {rows_copied} rows")

        # first stored background wins
        if not background_stored:
            try:
                r = src.execute("SELECT data FROM assets "
                                "WHERE key='background_frame'").fetchone()
                if r:
                    out.execute("INSERT OR REPLACE INTO assets VALUES "
                                "('background_frame', ?)", (r[0],))
                    background_stored = True
            except sqlite3.OperationalError:
                pass

        out.execute("INSERT INTO source_files VALUES (?,?,?,?,?)",
                    (os.path.basename(m['path']), id_offset, frame_offset,
                     m['start'].isoformat(), n_tracks))
        log(f"  + {os.path.basename(m['path'])}: {n_tracks} tracks "
            f"(ids {id_offset + 1}..{id_offset + max_tid}, "
            f"frames +{frame_offset})")
        id_offset += max_tid
        src.close()

    # ── Scene metadata for the merged site ──
    out.execute("DELETE FROM scene")
    scene = dict(metas[0]['meta'])
    scene.update({'video_start_time': site_start.isoformat(),
                  'total_frames': str(site_total_frames),
                  'fps': str(fps0),
                  'merged_from': str(len(metas))})
    out.executemany("INSERT INTO scene VALUES (?,?)", list(scene.items()))
    # merged file starts with a clean slate of gates
    for t in ('gates', 'gate_crossings'):
        try:
            out.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    out.commit()

    n = out.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    nobs = out.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    out.close()
    dur_h = (site_end - site_start.timestamp()) / 3600
    log(f"\nMerged {len(metas)} file(s) → {out_path}")
    log(f"  {n} tracks, {nobs} observations, site timeline "
        f"{site_start.strftime('%H:%M:%S')} + {dur_h:.1f} h")
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--traf-dir', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    try:
        merge(a.traf_dir.strip('"'), a.out.strip('"'))
    except RuntimeError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == '__main__':
    main()
