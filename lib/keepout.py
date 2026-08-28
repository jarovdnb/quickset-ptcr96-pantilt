#   Coupled pan/tilt keep-out model: the instrument's antennas can strike the
#   horizontal mounting bar at some pan/tilt combinations but not others.
#   Unlike pantilt.py's independent per-axis pan_min_abs/pan_max_abs/
#   tilt_min_abs/tilt_max_abs box, the safe tilt range here depends on pan,
#   so it can't be expressed as a simple box.
#
#   The envelope is defined by user-measured breakpoints
#   (pan_deg, tilt_min_deg, tilt_max_deg), piecewise-linearly interpolated
#   between them (flat beyond the outermost ones) -- the SAME model
#   implemented in pantilt_local/keepout_visualizer.html, which is meant to
#   preview exactly what this module enforces.
#
#   Indexed by pan (not tilt): at a fixed pan, the safe tilt band is a single
#   contiguous range for this mount's geometry (e.g. "blocked below tilt=60
#   deg, clear above it" at most pans, narrowing near the bar's own bearing).
#   Indexing by tilt instead would have produced two disjoint safe pan
#   windows at some tilts (the antenna clearing the bar only past either end
#   of it) -- a shape this piecewise-linear single-range-per-key model
#   can't express, and one where a straight-line sweep between two
#   individually-safe pan endpoints could cut back through the blocked
#   middle. Indexing by pan avoids that for this mount's actual geometry.
#
#   IMPORTANT: breakpoints are measured against RAW hardware angles (what
#   lib/qpt90.py's get_status() returns directly, via pt_keepout_record.py),
#   not the logical pan_abs/tilt_abs the dashboard shows. The two only differ
#   by the pan_invert/tilt_invert sign flip (see pantilt.py's flip()) --
#   callers must convert before calling into this module. The physical
#   bar/antenna geometry doesn't care about a software inversion setting;
#   only the raw encoder frame is physically meaningful.

import json
import os


def load_breakpoints(samples_path, bucket_size=5.0):
    """
    Load raw (tilt_deg, pan_deg) boundary samples recorded by
    pt_keepout_record.py and bucket them into (pan, tilt_min, tilt_max)
    breakpoints, sorted by pan. Returns [] if the file doesn't exist, is
    unreadable, or has fewer than 2 distinct pan buckets -- callers should
    treat an empty list as "no keep-out restriction configured" (nothing
    measured yet).
    """
    if not os.path.exists(samples_path):
        return []
    try:
        with open(samples_path, "r") as f:
            samples = json.load(f)
    except (OSError, ValueError):
        return []

    buckets = {}
    for s in samples:
        try:
            tilt = float(s["tilt_deg"])
            pan = float(s["pan_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        key = round(pan / bucket_size) * bucket_size
        buckets.setdefault(key, []).append(tilt)

    breakpoints = [
        {"pan": pan, "tilt_min": min(tilts), "tilt_max": max(tilts)}
        for pan, tilts in buckets.items()
    ]
    breakpoints.sort(key=lambda b: b["pan"])
    return breakpoints if len(breakpoints) >= 2 else []


def _normalize_pan(pan):
    return ((pan + 180.0) % 360.0 + 360.0) % 360.0 - 180.0


def safe_range_at_pan(pan, breakpoints):
    """
    (tilt_min, tilt_max) at this pan, linearly interpolated between the two
    nearest breakpoints (flat beyond the outermost ones). Returns
    (-90.0, 90.0) -- i.e. unrestricted -- if no breakpoints are configured.
    """
    if not breakpoints:
        return (-90.0, 90.0)
    pan = _normalize_pan(pan)
    if len(breakpoints) == 1:
        return (breakpoints[0]["tilt_min"], breakpoints[0]["tilt_max"])
    if pan <= breakpoints[0]["pan"]:
        b = breakpoints[0]
        return (b["tilt_min"], b["tilt_max"])
    last = breakpoints[-1]
    if pan >= last["pan"]:
        return (last["tilt_min"], last["tilt_max"])
    for a, b in zip(breakpoints, breakpoints[1:]):
        if a["pan"] <= pan <= b["pan"]:
            span = b["pan"] - a["pan"]
            f = 0.0 if span == 0 else (pan - a["pan"]) / span
            return (
                a["tilt_min"] + f * (b["tilt_min"] - a["tilt_min"]),
                a["tilt_max"] + f * (b["tilt_max"] - a["tilt_max"]),
            )
    return (-90.0, 90.0)   # unreachable given the bounds checks above


def is_safe(pan, tilt, breakpoints):
    """True if (pan, tilt) -- raw hardware angles -- is outside the keep-out zone."""
    lo, hi = safe_range_at_pan(pan, breakpoints)
    return lo <= tilt <= hi


def safe_tilt_intersection_over_sweep(pan_a, pan_b, breakpoints, steps=36):
    """
    The tilt range that stays safe at EVERY pan between pan_a and pan_b
    (inclusive), sampled at `steps` intervals. Used to find a single tilt
    value a pan-only move can hold through the whole sweep without ever
    entering the keep-out zone. Returns None if no such value exists (the
    move can't be done as a straight single-axis pan sweep at any fixed
    tilt -- the caller should refuse the move rather than guess a path).
    """
    lo, hi = -90.0, 90.0
    n = max(1, steps)
    for i in range(n + 1):
        p = pan_a + (pan_b - pan_a) * i / n
        r_lo, r_hi = safe_range_at_pan(p, breakpoints)
        lo = max(lo, r_lo)
        hi = min(hi, r_hi)
        if lo > hi:
            return None
    return (lo, hi)


def profile_path(keepout_dir, profile):
    #   Basename only -- never trust a profile name as a path
    name = os.path.basename(profile or "")
    return os.path.join(keepout_dir, f"{name}.json") if name else ""


def list_profiles(keepout_dir):
    if not os.path.isdir(keepout_dir):
        return []
    return sorted(f[:-5] for f in os.listdir(keepout_dir) if f.lower().endswith(".json"))
