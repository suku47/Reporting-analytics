"""
store_background.py — stamp the clean zero-detection frame into .traf files
so they become self-contained (viewer + trajectory plots need no video).

Single file:
    python store_background.py --traf "Results\\traf\\VID_x.traf" --video "VID_x.mp4"

Whole site (pairs videos to trafs by filename stem):
    python store_background.py --traf-dir "Results\\traf" --video-dir "E:\\site\\footage"
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.core.background_frame import capture_and_store_background


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--traf')
    ap.add_argument('--video')
    ap.add_argument('--traf-dir')
    ap.add_argument('--video-dir')
    a = ap.parse_args()

    pairs = []
    if a.traf and a.video:
        pairs = [(a.traf.strip('"'), a.video.strip('"'))]
    elif a.traf_dir and a.video_dir:
        vids = {}
        for ext in ('*.mp4', '*.avi', '*.mkv', '*.MP4'):
            for v in glob.glob(os.path.join(a.video_dir.strip('"'), ext)):
                vids[os.path.splitext(os.path.basename(v))[0].lower()] = v
        for t in sorted(glob.glob(os.path.join(a.traf_dir.strip('"'), '*.traf'))):
            stem = os.path.splitext(os.path.basename(t))[0].lower()
            if stem in vids:
                pairs.append((t, vids[stem]))
            else:
                print(f"  no matching video for {os.path.basename(t)} — skipped")
    else:
        ap.error('use --traf + --video, or --traf-dir + --video-dir')

    for traf, video in pairs:
        idx = capture_and_store_background(traf, video)
        name = os.path.basename(traf)
        print(f"  {name}: {'stored frame ' + str(idx) if idx is not None else 'FAILED'}")

    print(f"\nDone — {len(pairs)} file(s). These trafs now work without video.")


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
