"""
Class Profile Resolver - Auto-detect vehicle classification from .traf files.

The .traf scene table stores:
    class_map      = JSON: {"0": "Car", "1": "LGV", ...}
    class_full_map = JSON: {"0": "Cars", "1": "Light Goods Vehicle", ...}

This module reads those and provides:
    - Ordered list of vehicle class names (excluding non-vehicles)
    - Full display names for each class
    - Color assignments for each class
    - Non-vehicle class detection (Pedestrian, Cyclist, Bicycle)
    - Profile identification (us, uk, us_extended, or custom)

Usage:
    from app.core.class_profile import get_class_profile
    profile = get_class_profile(conn)
    print(profile['vehicle_classes'])   # ['Car', 'LGV', 'OGV1', ...]
    print(profile['excluded_classes'])  # {'Ped', 'Cyclist'}
    print(profile['profile_name'])     # 'uk'
"""

import json
import sqlite3


# Non-vehicle class names (case-insensitive matching)
NON_VEHICLE_NAMES = {
    'ped', 'peds', 'pedestrian',
    'cyclist', 'bicycle',
}

# Color palette: known class names get consistent colors
_KNOWN_COLORS = {
    # US profile
    'PV': '#58a6ff', 'SU': '#3fb950', 'CU': '#d29922',
    'MC': '#bc8cff', 'BUS': '#db6d28', 'PVT': '#f778ba',
    'Bicycle': '#79c0ff',
    # UK profile
    'Car': '#58a6ff', 'LGV': '#3fb950', 'OGV1': '#d29922',
    'OGV2': '#e3b341', 'Bus': '#db6d28', 'Biker': '#bc8cff',
    'Taxi': '#f778ba',
    # Non-vehicle (dimmer in UI)
    'Ped': '#f0883e', 'Peds': '#f0883e', 'Pedestrian': '#f0883e',
    'Cyclist': '#79c0ff',
}

# Fallback color cycle for unknown class names
_COLOR_CYCLE = [
    '#58a6ff', '#3fb950', '#d29922', '#bc8cff', '#db6d28',
    '#f778ba', '#79c0ff', '#e3b341', '#a5d6ff', '#7ee787',
    '#f9826c', '#d2a8ff',
]

# Known profiles for identification
_KNOWN_PROFILES = {
    'us': {'PV', 'SU', 'CU', 'MC', 'BUS'},
    'us_extended': {'PV', 'SU', 'CU', 'MC', 'BUS'},
    'uk': {'Car', 'LGV', 'OGV1', 'OGV2', 'Bus', 'Taxi'},
}


def get_class_profile(conn):
    """
    Read class profile from the .traf scene table.

    Returns dict with:
        profile_name:     str - 'us', 'uk', 'us_extended', or 'custom'
        all_classes:      list[str] - all class short names in order
        vehicle_classes:  list[str] - vehicle-only classes (for counting)
        excluded_classes: set[str] - non-vehicle class names
        class_full_names: dict[str, str] - short_name -> full display name
        class_colors:     dict[str, str] - short_name -> hex color
        id_to_name:       dict[int, str] - class_id -> short_name
        id_to_full:       dict[int, str] - class_id -> full_name
    """
    meta = {}
    try:
        for row in conn.execute("SELECT key, value FROM scene"):
            meta[row[0]] = row[1]
    except Exception:
        pass

    # Parse class maps from scene metadata
    id_to_name = {}
    id_to_full = {}

    raw_map = meta.get('class_map')
    if raw_map:
        try:
            parsed = json.loads(raw_map)
            id_to_name = {int(k): v for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError):
            pass

    raw_full = meta.get('class_full_map')
    if raw_full:
        try:
            parsed = json.loads(raw_full)
            id_to_full = {int(k): v for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: infer from tracks table
    if not id_to_name:
        id_to_name, id_to_full = _infer_from_tracks(conn)

    # Build ordered class list (sorted by class_id)
    all_classes = [id_to_name[k] for k in sorted(id_to_name.keys())]

    # Identify non-vehicle classes
    excluded = set()
    for cls in all_classes:
        if cls.lower() in NON_VEHICLE_NAMES:
            excluded.add(cls)

    vehicle_classes = [cls for cls in all_classes if cls not in excluded]

    # Build full name mapping
    class_full_names = {}
    for cid in sorted(id_to_name.keys()):
        short = id_to_name[cid]
        full = id_to_full.get(cid, short)
        class_full_names[short] = full

    # Assign colors
    class_colors = _assign_colors(all_classes)

    # Identify profile
    profile_name = _identify_profile(set(vehicle_classes))

    return {
        'profile_name': profile_name,
        'all_classes': all_classes,
        'vehicle_classes': vehicle_classes,
        'excluded_classes': excluded,
        'class_full_names': class_full_names,
        'class_colors': class_colors,
        'id_to_name': id_to_name,
        'id_to_full': id_to_full,
    }


def get_class_profile_json(conn):
    """Same as get_class_profile but JSON-serializable (sets -> lists)."""
    profile = get_class_profile(conn)
    profile['excluded_classes'] = sorted(profile['excluded_classes'])
    return profile


def _infer_from_tracks(conn):
    """Fallback: infer class map from tracks table data."""
    id_to_name = {}
    id_to_full = {}
    try:
        rows = conn.execute(
            "SELECT DISTINCT class_id, class_name, class_full_name "
            "FROM tracks WHERE class_name IS NOT NULL "
            "ORDER BY class_id"
        ).fetchall()
        for r in rows:
            cid = r[0]
            if cid is not None:
                id_to_name[cid] = r[1]
                id_to_full[cid] = r[2] or r[1]
    except Exception:
        pass

    if not id_to_name:
        id_to_name = {0: 'PV', 1: 'SU', 2: 'CU'}
        id_to_full = {0: 'Passenger Vehicle', 1: 'Single-Unit Truck',
                      2: 'Combination-Unit Truck'}

    return id_to_name, id_to_full


def _assign_colors(all_classes):
    """Assign colors: known mappings first, then cycle."""
    colors = {}
    cycle_idx = 0
    for cls in all_classes:
        if cls in _KNOWN_COLORS:
            colors[cls] = _KNOWN_COLORS[cls]
        else:
            colors[cls] = _COLOR_CYCLE[cycle_idx % len(_COLOR_CYCLE)]
            cycle_idx += 1
    return colors


def _identify_profile(vehicle_set):
    """Match vehicle class set to a known profile."""
    for name, known_set in _KNOWN_PROFILES.items():
        if vehicle_set == known_set or vehicle_set.issubset(known_set):
            return name
        overlap = len(vehicle_set & known_set)
        if overlap >= len(known_set) * 0.7:
            return name
    return 'custom'
