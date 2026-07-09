"""
track_visualizer.py — Professional traffic analysis video overlay

DataFromSky-inspired visualization with:
  - Color-coded bounding boxes by vehicle class
  - ID label with class name and confidence score
  - Fading trajectory trail with gradient thickness
  - Speed indicator
  - Stationary/moving badge
  - Professional HUD overlay (frame counter, active/total, timestamp)
  - Gate lines with crossing counts

Usage standalone:
  python track_visualizer.py --video input.mp4 --traf analysis.traf --output overlay.mp4

Or import in video_processor.py:
  from track_visualizer import TrackOverlayRenderer
"""

import cv2
import numpy as np
import json
import sqlite3
import os
import sys
import logging
from collections import deque

# ──────────────────────────────────────────────────────────
# COLOR SCHEME — distinct from DataFromSky's red
# ──────────────────────────────────────────────────────────

# BGR format for OpenCV
CLASS_COLORS = {
    'PV':  (255, 180, 50),   # Bright cyan-blue
    'SU':  (50, 200, 80),    # Green
    'CU':  (30, 160, 255),   # Orange
    'MC':  (200, 120, 255),  # Purple/pink
    'BUS': (60, 220, 220),   # Yellow
    'UNK': (180, 180, 180),  # Gray
}

# Softer trail colors (dimmed version of class colors)
TRAIL_COLORS = {
    'PV':  (180, 130, 40),
    'SU':  (40, 150, 60),
    'CU':  (25, 120, 200),
    'MC':  (150, 90, 200),
    'BUS': (50, 170, 170),
    'UNK': (130, 130, 130),
}

# HUD colors
HUD_BG = (30, 30, 30)
HUD_TEXT = (240, 240, 240)
HUD_ACCENT = (255, 180, 50)

# Label background with transparency
LABEL_BG_ALPHA = 0.85


def get_class_color(class_name):
    return CLASS_COLORS.get(class_name, CLASS_COLORS['UNK'])


def get_trail_color(class_name):
    return TRAIL_COLORS.get(class_name, TRAIL_COLORS['UNK'])


# ──────────────────────────────────────────────────────────
# DRAWING HELPERS
# ──────────────────────────────────────────────────────────

def draw_label_box(frame, text, org, color, font_scale=0.45, thickness=1):
    """Draw text with a filled background box."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)
    x, y = int(org[0]), int(org[1])
    pad = 3

    # Background rectangle with slight transparency
    overlay = frame.copy()
    cv2.rectangle(overlay, (x - pad, y - th - pad - 2),
                  (x + tw + pad, y + baseline + pad), color, -1)
    cv2.addWeighted(overlay, LABEL_BG_ALPHA, frame, 1 - LABEL_BG_ALPHA, 0, frame)

    # Text (white or black depending on background brightness)
    brightness = color[0] * 0.114 + color[1] * 0.587 + color[2] * 0.299
    txt_color = (0, 0, 0) if brightness > 140 else (255, 255, 255)
    cv2.putText(frame, text, (x, y), font, font_scale, txt_color, thickness, cv2.LINE_AA)


def draw_trail(frame, trail_points, color, max_thickness=3):
    """Draw a fading trail with gradient thickness."""
    n = len(trail_points)
    if n < 2:
        return
    for i in range(1, n):
        alpha = i / n  # 0→1 (old→new)
        t = max(1, int(alpha * max_thickness))
        c = tuple(int(color[j] * (0.3 + 0.7 * alpha)) for j in range(3))
        cv2.line(frame, trail_points[i - 1], trail_points[i], c, t, cv2.LINE_AA)


def draw_bbox(frame, bbox, color, thickness=2):
    """Draw rounded-corner-style bounding box."""
    x1, y1, x2, y2 = map(int, bbox)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness, cv2.LINE_AA)

    # Corner accents (thicker short lines at corners)
    corner_len = min(15, (x2 - x1) // 4, (y2 - y1) // 4)
    t = thickness + 1
    # Top-left
    cv2.line(frame, (x1, y1), (x1 + corner_len, y1), color, t)
    cv2.line(frame, (x1, y1), (x1, y1 + corner_len), color, t)
    # Top-right
    cv2.line(frame, (x2, y1), (x2 - corner_len, y1), color, t)
    cv2.line(frame, (x2, y1), (x2, y1 + corner_len), color, t)
    # Bottom-left
    cv2.line(frame, (x1, y2), (x1 + corner_len, y2), color, t)
    cv2.line(frame, (x1, y2), (x1, y2 - corner_len), color, t)
    # Bottom-right
    cv2.line(frame, (x2, y2), (x2 - corner_len, y2), color, t)
    cv2.line(frame, (x2, y2), (x2, y2 - corner_len), color, t)


def draw_centroid(frame, cx, cy, color, radius=4):
    """Draw centroid dot."""
    cv2.circle(frame, (int(cx), int(cy)), radius, color, -1, cv2.LINE_AA)
    cv2.circle(frame, (int(cx), int(cy)), radius + 1, (255, 255, 255), 1, cv2.LINE_AA)


def draw_hud(frame, frame_idx, active, total, fps=30, timestamp=None):
    """Draw professional HUD overlay."""
    h, w = frame.shape[:2]

    # Top-left info panel
    panel_h = 65
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (280, panel_h), HUD_BG, -1)
    cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

    cv2.putText(frame, f"Frame: {frame_idx}", (12, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, HUD_ACCENT, 1, cv2.LINE_AA)
    cv2.putText(frame, f"Active: {active}   Total: {total}", (12, 48),
                cv2.FONT_HERSHEY_SIMPLEX, 0.50, HUD_TEXT, 1, cv2.LINE_AA)

    # Timestamp (if available)
    if timestamp:
        ts_w = 300
        overlay2 = frame.copy()
        cv2.rectangle(overlay2, (w - ts_w - 10, h - 35), (w, h), HUD_BG, -1)
        cv2.addWeighted(overlay2, 0.6, frame, 0.4, 0, frame)
        cv2.putText(frame, timestamp, (w - ts_w, h - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, HUD_TEXT, 1, cv2.LINE_AA)


def draw_gate(frame, gate, count=None):
    """Draw gate line on frame."""
    x1, y1, x2, y2 = int(gate['x1']), int(gate['y1']), int(gate['x2']), int(gate['y2'])
    color = (0, 100, 255)  # Orange-red

    cv2.line(frame, (x1, y1), (x2, y2), color, 3, cv2.LINE_AA)
    cv2.circle(frame, (x1, y1), 5, color, -1)
    cv2.circle(frame, (x2, y2), 5, color, -1)

    mx, my = (x1 + x2) // 2, (y1 + y2) // 2
    label = gate.get('name', 'Gate')
    if count is not None:
        label += f" ({count})"
    draw_label_box(frame, label, (mx - 30, my - 5), color, font_scale=0.5, thickness=1)


# ──────────────────────────────────────────────────────────
# MAIN RENDERER CLASS
# ──────────────────────────────────────────────────────────

class TrackOverlayRenderer:
    """
    Renders professional track overlay on video frames.
    
    Can be used:
    1. By video_processor.py during processing
    2. Standalone to re-render from a .traf file
    """

    def __init__(self, trail_length=40, show_confidence=True,
                 show_speed=True, show_class=True):
        self.trail_length = trail_length
        self.show_confidence = show_confidence
        self.show_speed = show_speed
        self.show_class = show_class
        self.trail_history = {}  # track_id → deque of (cx, cy)
        self.class_cache = {}    # track_id → class_name

    def draw_track_on_frame(self, frame, track_id, centroid, bbox=None,
                            class_name='PV', confidence=None, speed=None,
                            is_stationary=False):
        """Draw a single tracked vehicle on a frame."""
        cx, cy = int(centroid[0]), int(centroid[1])
        color = get_class_color(class_name)
        trail_color = get_trail_color(class_name)

        # Update trail
        if track_id not in self.trail_history:
            self.trail_history[track_id] = deque(maxlen=self.trail_length)
        self.trail_history[track_id].append((cx, cy))
        self.class_cache[track_id] = class_name

        # Draw trail
        trail = list(self.trail_history[track_id])
        draw_trail(frame, trail, trail_color, max_thickness=3)

        # Draw bounding box
        if bbox is not None:
            draw_bbox(frame, bbox, color, thickness=2)

        # Draw centroid
        draw_centroid(frame, cx, cy, color)

        # Build label
        label_parts = [f"id: {track_id}"]
        if self.show_class:
            label_parts = [f"{class_name} #{track_id}"]

        label = ' '.join(label_parts)

        # Position label above bbox or centroid
        if bbox is not None:
            lx, ly = int(bbox[0]), int(bbox[1]) - 8
        else:
            lx, ly = cx - 20, cy - 18

        draw_label_box(frame, label, (lx, ly), color, font_scale=0.42, thickness=1)

        # Confidence score (second line)
        if self.show_confidence and confidence is not None:
            conf_text = f"score: {confidence:.0f}%"
            draw_label_box(frame, conf_text, (lx, ly + 16), color, font_scale=0.35, thickness=1)

        # Speed or stationary badge
        if is_stationary:
            draw_label_box(frame, "STAT", (lx, ly + 30),
                          (0, 0, 200), font_scale=0.35, thickness=1)
        elif self.show_speed and speed is not None and speed > 0.5:
            spd_text = f"{speed:.1f} px/f"
            draw_label_box(frame, spd_text, (lx, ly + 30),
                          (80, 80, 80), font_scale=0.3, thickness=1)

    def render_frame(self, frame, frame_idx, tracks_this_frame, total_vehicles,
                     gates=None, gate_counts=None, timestamp=None):
        """
        Render complete overlay for one frame.
        
        tracks_this_frame: list of dicts with keys:
            track_id, centroid(x,y), bbox(x1,y1,x2,y2), class_name,
            confidence, speed, is_stationary
        """
        # Draw gates first (behind tracks)
        if gates:
            for g in gates:
                count = gate_counts.get(g.get('gate_id')) if gate_counts else None
                draw_gate(frame, g, count)

        # Draw each active track
        for t in tracks_this_frame:
            self.draw_track_on_frame(
                frame,
                track_id=t['track_id'],
                centroid=t['centroid'],
                bbox=t.get('bbox'),
                class_name=t.get('class_name', 'PV'),
                confidence=t.get('confidence'),
                speed=t.get('speed'),
                is_stationary=t.get('is_stationary', False),
            )

        # Draw HUD
        draw_hud(frame, frame_idx, len(tracks_this_frame),
                 total_vehicles, timestamp=timestamp)

        return frame


# ──────────────────────────────────────────────────────────
# STANDALONE: Render overlay from .traf + video
# ──────────────────────────────────────────────────────────

def render_from_traf(traf_path, video_path, output_path, fps=None):
    """
    Create overlay video from .traf file + source video.
    """
    conn = sqlite3.connect(traf_path)
    conn.row_factory = sqlite3.Row

    # Load metadata
    meta = {r['key']: r['value'] for r in conn.execute("SELECT key, value FROM scene")}
    if fps is None:
        fps = float(meta.get('fps', 30.0))

    # Load all observations indexed by frame
    print("Loading observations...")
    frame_index = {}
    rows = conn.execute(
        "SELECT o.track_id, o.frame, o.cx, o.cy, o.bbox_x1, o.bbox_y1, "
        "o.bbox_x2, o.bbox_y2, o.speed_px, o.observed, "
        "t.class_name, t.is_stationary, t.track_quality "
        "FROM observations o "
        "JOIN tracks t ON o.track_id = t.track_id "
        "ORDER BY o.frame"
    ).fetchall()

    for r in rows:
        f = r['frame']
        if f not in frame_index:
            frame_index[f] = []
        entry = {
            'track_id': r['track_id'],
            'centroid': (r['cx'], r['cy']),
            'class_name': r['class_name'] or 'PV',
            'speed': r['speed_px'],
            'is_stationary': bool(r['is_stationary']),
            'confidence': (r['track_quality'] or 0.8) * 100,
        }
        if r['bbox_x1'] is not None:
            entry['bbox'] = (r['bbox_x1'], r['bbox_y1'], r['bbox_x2'], r['bbox_y2'])
        frame_index[f].append(entry)

    total_vehicles = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    # Load gates
    gates = [dict(r) for r in conn.execute("SELECT * FROM gates")]
    gate_counts = {}
    for g in gates:
        cnt = conn.execute("SELECT COUNT(*) FROM gate_crossings WHERE gate_id=?",
                           (g['gate_id'],)).fetchone()[0]
        gate_counts[g['gate_id']] = cnt

    conn.close()

    # Open video
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: Cannot open video {video_path}")
        return

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    out = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*"mp4v"),
                          fps, (w, h))

    renderer = TrackOverlayRenderer(trail_length=40)

    print(f"Rendering: {total_frames} frames, {total_vehicles} tracks")
    frame_idx = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1

        tracks = frame_index.get(frame_idx, [])
        renderer.render_frame(frame, frame_idx, tracks, total_vehicles,
                              gates=gates, gate_counts=gate_counts)
        out.write(frame)

        if frame_idx % 100 == 0:
            print(f"  Frame {frame_idx}/{total_frames}")

    cap.release()
    out.release()
    print(f"Saved: {output_path}")


# ──────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Render track overlay video")
    parser.add_argument('--video', required=True, help='Source video')
    parser.add_argument('--traf', required=True, help='.traf file')
    parser.add_argument('--output', required=True, help='Output video path')
    parser.add_argument('--fps', type=float, help='Override FPS')
    args = parser.parse_args()

    render_from_traf(args.traf, args.video, args.output, fps=args.fps)
