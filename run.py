"""
TrafficAnalyticsViewer — Entry point

Usage:
  python run.py --traf results/video.traf --video results/video.mp4
  python run.py --traf results/video.etraf --video results/video.mp4 --key TRAF-XXXX-YYYY-ZZZZ
  python run.py --traf results/video.traf --host 0.0.0.0 --port 8080  (web deployment)
"""

import argparse
import threading
import webbrowser

import uvicorn

from app.server import app, load_traf, load_video, load_image


def main():
    parser = argparse.ArgumentParser(
        description="Traffic Analytics Viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Desktop:   python run.py --traf analysis.traf --video video.mp4
  Encrypted: python run.py --traf analysis.etraf --video video.mp4 --key TRAF-A1B2-C3D4-E5F6
  Web:       python run.py --traf analysis.traf --host 0.0.0.0 --port 8080 --no-browser
        """)
    parser.add_argument('--traf', required=True, help='.traf or .etraf file path')
    parser.add_argument('--video', help='Source video file path')
    parser.add_argument('--image', help='Static background image (instead of video)')
    parser.add_argument('--key', help='License key (required for .etraf files)')
    parser.add_argument('--port', type=int, default=8000, help='Server port (default: 8000)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Host address (use 0.0.0.0 for network access)')
    parser.add_argument('--no-browser', action='store_true',
                        help="Don't auto-open browser")
    args = parser.parse_args()

    # Load data
    load_traf(args.traf, license_key=args.key)
    if args.video:
        load_video(args.video)
    if args.image:
        load_image(args.image)

    # Auto-open browser
    url = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"\n  Traffic Analytics Viewer")
    print(f"  ───────────────────────")
    print(f"  URL:  {url}")
    print(f"  Press Ctrl+C to stop\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
