#   Plain-assert tests for lib/pantilt_program.py (no hardware needed).
#   Run with: python3 tests/test_pantilt_program.py

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.pantilt_program import (ProgramError, load_program, relative_limits,
                                 validate_points, validate_schedule, pair_min_gap,
                                 next_occurrence, summarize, preview, upcoming_sweeps,
                                 home_orientation_warnings, home_orientation_overrides,
                                 backfill_recorded_setup, dump_program)

PANTILT_CFG = {
    "pan_min_abs": -180.0, "pan_max_abs": 180.0,
    "tilt_min_abs": -90.0, "tilt_max_abs": 90.0,
    "home_pan_abs": 10.0, "home_tilt_abs": -5.0,
    "warn_margin_deg": 2.0, "min_gap_seconds": 60,
    "speed_deg_per_s_estimate": 4.0, "settle_seconds": 2,
}

SINGLE = """
schema_version: 1
type: single
defaults:
  try: 3
points:
  - pan_deg: 0
    tilt_deg: 0
  - pan_deg: 15
    tilt_deg: 0
"""

AUTOMATED = """
schema_version: 1
type: automated
initial_startdate_pantilt: '2025-04-07 12:00:00'
defaults:
  try: 3
points:
  - pan_deg: 0
    tilt_deg: 0
    repeated_minutes: 30
    offset_sec: 0
  - pan_deg: 15
    tilt_deg: 0
    repeated_minutes: 60
    offset_sec: 120
"""

#   Two independently-scheduled sweeps: hourly (anchored on the hour) and
#   daily (anchored 3h07 later, so its grid never exactly coincides with the
#   hourly one -- see test_sweeps for what happens when it does)
SWEEPS = """
schema_version: 1
type: single
defaults:
  try: 2
sweeps:
  - name: hourly
    initial_startdate_pantilt: '2026-08-21 00:00:00'
    repeat_minutes: 60
    points:
      - {pan_deg: 0, tilt_deg: 0}
      - {pan_deg: 5, tilt_deg: 0}
  - name: daily
    initial_startdate_pantilt: '2026-08-21 03:07:00'
    repeat_minutes: 1440
    points:
      - {pan_deg: -10, tilt_deg: 0}
      - {pan_deg: 10, tilt_deg: 0}
"""


def expect_error(text, fragment):
    try:
        load_program(text)
        assert False, f"expected ProgramError containing '{fragment}'"
    except ProgramError as e:
        assert fragment in str(e), f"'{fragment}' not in '{e}'"


def test_load_program():
    prog = load_program(SINGLE)
    assert prog["type"] == "single" and prog["defaults"]["try"] == 3 and len(prog["points"]) == 2

    expect_error("schema_version: 2\ntype: single\npoints: [{pan_deg: 0, tilt_deg: 0}]", "schema_version")
    expect_error("schema_version: 1\ntype: nope\npoints: [{pan_deg: 0, tilt_deg: 0}]", "type")
    expect_error("schema_version: 1\ntype: single\npoints: []", "points")
    expect_error("schema_version: 1\ntype: single\npoints: [{pan_deg: a, tilt_deg: 0}]", "pan_deg")
    START = "initial_startdate_pantilt: '2025-04-07 12:00:00'\n"
    expect_error("schema_version: 1\ntype: automated\n" + START + "points: [{pan_deg: 0, tilt_deg: 0}]", "repeated_minutes")
    expect_error("schema_version: 1\ntype: automated\n" + START +
                 "points: [{pan_deg: 0, tilt_deg: 0, repeated_minutes: 30, offset_sec: 1800}]", "offset_sec")
    expect_error("schema_version: 1\ntype: automated\npoints: [{pan_deg: 0, tilt_deg: 0, repeated_minutes: 30}]",
                 "initial_startdate_pantilt")
    expect_error("schema_version: 1\ntype: automated\ninitial_startdate_pantilt: 'not a date'\n"
                 "points: [{pan_deg: 0, tilt_deg: 0, repeated_minutes: 30}]", "initial_startdate_pantilt")
    expect_error("not: [valid", "YAML")

    #   Confirm it's parsed into a UTC epoch regardless of quoting
    prog = load_program(AUTOMATED)
    assert prog["initial_startdate_pantilt"] == "2025-04-07 12:00:00"
    from datetime import datetime, timezone
    assert prog["initial_startdate_epoch"] == int(datetime(2025, 4, 7, 12, 0, 0, tzinfo=timezone.utc).timestamp())


def test_relative_limits():
    #   Home at abs (10, -5) shifts the reachable relative window
    limits = relative_limits(PANTILT_CFG)
    assert limits == {"pan_min": -190.0, "pan_max": 170.0, "tilt_min": -85.0, "tilt_max": 95.0}

    #   Asymmetric raw limits: inverting must negate AND swap min/max, since
    #   pan_min_abs/pan_max_abs describe the physical (raw) travel range,
    #   not the logical one. A no-op with pan_invert absent/0 (regression check).
    asym_cfg = dict(PANTILT_CFG, pan_min_abs=-90.0, pan_max_abs=170.0, home_pan_abs=0.0)
    assert relative_limits(asym_cfg)["pan_min"] == -90.0
    assert relative_limits(asym_cfg)["pan_max"] == 170.0

    inverted_cfg = dict(asym_cfg, pan_invert=1)
    inv_limits = relative_limits(inverted_cfg)
    assert inv_limits["pan_min"] == -170.0 and inv_limits["pan_max"] == 90.0

    #   Home position still shifts the (already-inverted) logical window
    inverted_home_cfg = dict(inverted_cfg, home_pan_abs=10.0)
    inv_home_limits = relative_limits(inverted_home_cfg)
    assert inv_home_limits["pan_min"] == -180.0 and inv_home_limits["pan_max"] == 80.0


def test_validate_points():
    prog = load_program("""
schema_version: 1
type: single
points:
  - {pan_deg: 0, tilt_deg: 0}       # ok
  - {pan_deg: 170, tilt_deg: 0}     # exactly on the pan limit -> warn
  - {pan_deg: 171, tilt_deg: 0}     # outside -> error
  - {pan_deg: 0, tilt_deg: 94}      # inside warn margin -> warn
""")
    reports = validate_points(prog, PANTILT_CFG)
    assert [r["status"] for r in reports] == ["ok", "warn", "error", "warn"]
    assert "outside relative limits" in reports[2]["message"]


def test_pair_min_gap():
    #   Same period, offsets 120 s apart -> 120 s
    assert pair_min_gap(1800, 0, 1800, 120) == 120
    #   30 min vs 60 min, offsets 0 and 120 -> they meet within 120 s every hour
    assert pair_min_gap(1800, 0, 3600, 120) == 120
    #   Brute-force cross-check over 48 h for random period/offset sets
    random.seed(42)
    for _ in range(50):
        p1 = random.randint(1, 48) * 60
        p2 = random.randint(1, 48) * 60
        o1 = random.randint(0, p1 - 1)
        o2 = random.randint(0, p2 - 1)
        times1 = [o1 + k * p1 for k in range(0, 48 * 3600 // p1)]
        times2 = [o2 + k * p2 for k in range(0, 48 * 3600 // p2)]
        brute = min(abs(t1 - t2) for t1 in times1 for t2 in times2)
        if p1 * p2 // math.gcd(p1, p2) + max(p1, p2) <= 48 * 3600:
            #   Window covers a full repeat cycle -> formula must match exactly
            assert pair_min_gap(p1, o1, p2, o2) == brute, (p1, o1, p2, o2)
        else:
            assert pair_min_gap(p1, o1, p2, o2) <= brute, (p1, o1, p2, o2)


def test_validate_schedule():
    #   The user's example program: offsets 120 s apart at the top of the hour
    prog = load_program(AUTOMATED)
    conflicts = validate_schedule(prog, dict(PANTILT_CFG, min_gap_seconds=60))
    assert conflicts == []

    conflicts = validate_schedule(prog, dict(PANTILT_CFG, min_gap_seconds=180))
    assert len(conflicts) == 1 and conflicts[0]["points"] == [0, 1] and conflicts[0]["gap_seconds"] == 120

    #   A point repeating faster than the minimum gap conflicts with itself
    fast = load_program("""
schema_version: 1
type: automated
initial_startdate_pantilt: '2025-04-07 12:00:00'
points:
  - {pan_deg: 0, tilt_deg: 0, repeated_minutes: 1, offset_sec: 0}
""")
    conflicts = validate_schedule(fast, dict(PANTILT_CFG, min_gap_seconds=90))
    assert len(conflicts) == 1 and conflicts[0]["points"] == [0]


def test_next_occurrence():
    point = {"repeated_minutes": 30, "offset_sec": 120}
    #   Grid: 120, 1920, 3720, ... anchored at the unix epoch (default ref_epoch=0)
    assert next_occurrence(point, 0) == 120
    assert next_occurrence(point, 119) == 120
    assert next_occurrence(point, 120) == 1920      # strictly after now
    assert next_occurrence(point, 1000000) == 1000920
    assert (next_occurrence(point, 1000000) - 120) % 1800 == 0

    #   With a program reference date, the grid shifts to be anchored there instead
    ref = 500000
    assert next_occurrence(point, ref, ref) == ref + 120
    assert next_occurrence(point, ref + 120, ref) == ref + 1920
    assert (next_occurrence(point, 1000000, ref) - (ref + 120)) % 1800 == 0


def test_summarize_and_preview():
    summary = summarize(load_program(SINGLE), PANTILT_CFG)
    assert summary["n_points"] == 2 and summary["estimated_minutes"] > 0

    summary = summarize(load_program(AUTOMATED), PANTILT_CFG)
    assert summary["measurements_per_hour"] == 3.0   # every 30 min + every 60 min

    result = preview(AUTOMATED, PANTILT_CFG)
    assert result["valid"] and len(result["timeline"]) > 0

    result = preview(SINGLE.replace("pan_deg: 15", "pan_deg: 179"), PANTILT_CFG)
    assert not result["valid"]


def test_recorded_setup():
    #   Optional per the docstring: a program with no home/orientation fields
    #   loads fine, warns about nothing, and its overrides are empty
    prog = load_program(SINGLE)
    assert "home_pan_abs" not in prog["defaults"] and "pan_orientation" not in prog["defaults"]
    assert home_orientation_warnings(prog, PANTILT_CFG) == []
    assert home_orientation_overrides(prog, PANTILT_CFG) == {}

    #   Bad values are rejected the same way any other field is
    expect_error("schema_version: 1\ntype: single\ndefaults: {home_pan_abs: not_a_number}\n"
                 "points: [{pan_deg: 0, tilt_deg: 0}]", "home_pan_abs")
    expect_error("schema_version: 1\ntype: single\ndefaults: {pan_orientation: 2}\n"
                 "points: [{pan_deg: 0, tilt_deg: 0}]", "pan_orientation")

    #   Declared and different from the current setup -> a warning per differing field
    declared = load_program("""
schema_version: 1
type: single
defaults:
  try: 1
  home_pan_abs: 20.0
  home_tilt_abs: -5.0
  pan_orientation: -1
  tilt_orientation: 1
points:
  - {pan_deg: 0, tilt_deg: 0}
""")
    cfg = dict(PANTILT_CFG, home_pan_abs=10.0, home_tilt_abs=-5.0, pan_invert=0, tilt_invert=0)
    warnings = home_orientation_warnings(declared, cfg)
    assert len(warnings) == 2  # home_pan_abs (10 -> 20) and pan_orientation (1 -> -1); tilt matches on both
    overrides = home_orientation_overrides(declared, cfg)
    assert overrides == {"home_pan_abs": 20.0, "home_tilt_abs": -5.0, "pan_invert": 1, "tilt_invert": 0}

    #   Matching setup -> no warnings, but overrides are still returned (idempotent re-apply)
    cfg_matching = dict(PANTILT_CFG, home_pan_abs=20.0, home_tilt_abs=-5.0, pan_invert=1, tilt_invert=0)
    assert home_orientation_warnings(declared, cfg_matching) == []

    #   Backfill only fills what's missing, and never touches an already-declared field
    partial = load_program("""
schema_version: 1
type: single
defaults:
  try: 1
  home_pan_abs: 99.0
points:
  - {pan_deg: 0, tilt_deg: 0}
""")
    backfill_recorded_setup(partial, dict(PANTILT_CFG, home_pan_abs=10.0, home_tilt_abs=-5.0,
                                          pan_invert=1, tilt_invert=0))
    assert partial["defaults"]["home_pan_abs"] == 99.0          # untouched: file already declared it
    assert partial["defaults"]["home_tilt_abs"] == -5.0          # backfilled from current config
    assert partial["defaults"]["pan_orientation"] == -1          # backfilled: pan_invert=1 -> -1
    assert partial["defaults"]["tilt_orientation"] == 1          # backfilled: tilt_invert=0 -> 1
    assert "recorded_settings" in partial and partial["recorded_settings"]["pan_max_speed"] == 64

    #   Round-trips through YAML and is still a valid, loadable program
    text = dump_program(partial)
    reloaded = load_program(text)
    assert reloaded["defaults"]["home_pan_abs"] == 99.0
    assert reloaded["defaults"]["pan_orientation"] == -1
    assert "initial_startdate_epoch" not in text  # internal/derived field is not persisted


def test_repeat_minutes():
    #   Plain single: repeat_minutes is absent, not just falsy
    plain = load_program(SINGLE)
    assert plain.get("repeat_minutes") is None
    assert "repeat_minutes" not in dump_program(plain)

    #   single + repeat_minutes needs initial_startdate_pantilt, same as automated
    expect_error("""
schema_version: 1
type: single
repeat_minutes: 30
defaults: {try: 1}
points: [{pan_deg: 0, tilt_deg: 0}]
""", "initial_startdate_pantilt")

    #   repeat_minutes must be a positive int
    expect_error("""
schema_version: 1
type: single
repeat_minutes: 0
initial_startdate_pantilt: '2025-04-07 12:00:00'
defaults: {try: 1}
points: [{pan_deg: 0, tilt_deg: 0}]
""", "repeat_minutes must be an integer")

    #   automated ignores a stray repeat_minutes key rather than misparsing it
    auto = load_program(AUTOMATED + "repeat_minutes: 15\n")
    assert auto.get("repeat_minutes") is None

    #   Valid case, and it round-trips through dump/load unchanged
    repeating = load_program("""
schema_version: 1
type: single
repeat_minutes: 30
initial_startdate_pantilt: '2025-04-07 12:00:00'
defaults: {try: 1}
points:
  - {pan_deg: 0, tilt_deg: 0}
  - {pan_deg: 5, tilt_deg: 0}
""")
    assert repeating["repeat_minutes"] == 30
    assert repeating["initial_startdate_epoch"] == 1744027200

    #   The whole sweep is scheduled like a single automated point (offset_sec=0)
    synthetic = {"repeated_minutes": repeating["repeat_minutes"], "offset_sec": 0}
    ref_epoch = repeating["initial_startdate_epoch"]
    due = next_occurrence(synthetic, ref_epoch + 10 * 60, ref_epoch)   # 10 min after start
    assert due == ref_epoch + 30 * 60   # next sweep starts at the 30-min mark, not 10+30

    reloaded = load_program(dump_program(repeating))
    assert reloaded["repeat_minutes"] == 30

    #   summarize() warns if one sweep wouldn't fit inside its own repeat interval:
    #   two points at ~37s each (settle + MEASURE_SECONDS_ESTIMATE) is >1 minute
    tight = load_program("""
schema_version: 1
type: single
repeat_minutes: 1
initial_startdate_pantilt: '2025-04-07 12:00:00'
defaults: {try: 1}
points:
  - {pan_deg: 0, tilt_deg: 0}
  - {pan_deg: 5, tilt_deg: 0}
""")
    summary = summarize(tight, PANTILT_CFG)
    assert summary["repeat_minutes"] == 1
    assert any("exceeds repeat_minutes" in w for w in summary.get("warnings", []))

    #   ...and doesn't warn when it comfortably fits
    summary = summarize(repeating, PANTILT_CFG)
    assert summary["repeat_minutes"] == 30
    assert "warnings" not in summary or not summary["warnings"]


def test_sweeps():
    prog = load_program(SWEEPS)
    assert prog["type"] == "single"
    assert "points" not in prog
    assert len(prog["sweeps"]) == 2
    assert prog["sweeps"][0]["name"] == "hourly" and prog["sweeps"][0]["repeat_minutes"] == 60
    assert prog["sweeps"][1]["name"] == "daily" and prog["sweeps"][1]["repeat_minutes"] == 1440
    assert len(prog["sweeps"][0]["points"]) == 2 and len(prog["sweeps"][1]["points"]) == 2
    assert prog["sweeps"][0]["initial_startdate_epoch"] < prog["sweeps"][1]["initial_startdate_epoch"]

    #   'sweeps' is single-only
    expect_error("""
schema_version: 1
type: automated
sweeps:
  - {name: a, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
  - {name: b, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
""", "sweeps")

    #   Can't mix 'sweeps' with a top-level 'points'
    expect_error("""
schema_version: 1
type: single
points: [{pan_deg: 0, tilt_deg: 0}]
sweeps:
  - {name: a, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
  - {name: b, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
""", "not both")

    #   Can't mix 'sweeps' with a top-level repeat_minutes/initial_startdate_pantilt
    expect_error("""
schema_version: 1
type: single
repeat_minutes: 30
initial_startdate_pantilt: '2025-01-01 00:00:00'
sweeps:
  - {name: a, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
  - {name: b, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
""", "per-sweep")

    #   A single-entry 'sweeps' list is valid (equivalent to plain 'points:',
    #   but keeps the sweeps: shape -- e.g. when trimming a sweep out of a
    #   larger multi-sweep file without rewriting it)
    solo = load_program("""
schema_version: 1
type: single
sweeps:
  - {name: a, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
""")
    assert len(solo["sweeps"]) == 1 and solo["sweeps"][0]["name"] == "a"

    #   Empty 'sweeps' list is still rejected
    expect_error("""
schema_version: 1
type: single
sweeps: []
""", "non-empty")

    #   Duplicate sweep names rejected
    expect_error("""
schema_version: 1
type: single
sweeps:
  - {name: a, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
  - {name: a, repeat_minutes: 2, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
""", "used more than once")

    #   Each sweep needs its own repeat_minutes >= 1...
    expect_error("""
schema_version: 1
type: single
sweeps:
  - {name: a, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
  - {name: b, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
""", "repeat_minutes")

    #   ...and its own initial_startdate_pantilt
    expect_error("""
schema_version: 1
type: single
sweeps:
  - {name: a, repeat_minutes: 1, points: [{pan_deg: 0, tilt_deg: 0}]}
  - {name: b, repeat_minutes: 1, initial_startdate_pantilt: '2025-01-01 00:00:00', points: [{pan_deg: 0, tilt_deg: 0}]}
""", "initial_startdate_pantilt")

    #   summarize(): per-sweep breakdown; the two grids here don't come close
    #   enough to overlap given how short each sweep is
    summary = summarize(prog, PANTILT_CFG)
    assert summary["n_sweeps"] == 2 and summary["n_points"] == 4
    assert [sw["name"] for sw in summary["sweeps"]] == ["hourly", "daily"]
    assert summary["sweeps"][0]["repeat_minutes"] == 60
    assert not any(sw.get("warnings") for sw in summary["sweeps"])
    assert summary["sweep_conflicts"] == []

    #   validate_points()/preview() tag each point with its sweep
    reports = validate_points(prog, PANTILT_CFG)
    assert len(reports) == 4
    assert reports[0]["sweep_name"] == "hourly" and reports[2]["sweep_name"] == "daily"

    result = preview(SWEEPS, PANTILT_CFG)
    assert result["valid"] and len(result["timeline"]) > 0
    assert all("sweep_name" in t for t in result["timeline"])

    #   validate_schedule() stays automated-only: sweeps never produce a hard conflict
    assert validate_schedule(prog, PANTILT_CFG) == []

    #   Round-trips through dump/load; per-sweep epoch is stripped, not persisted
    text = dump_program(prog)
    assert "initial_startdate_epoch" not in text
    reloaded = load_program(text)
    assert len(reloaded["sweeps"]) == 2 and reloaded["sweeps"][0]["name"] == "hourly"

    #   upcoming_sweeps(): each sweep fires on its own grid, timeline tags the name
    ref = prog["sweeps"][0]["initial_startdate_epoch"]
    events = upcoming_sweeps(prog, ref, horizon_s=3600)
    assert events[0]["sweep_name"] == "hourly"

    #   Two sweeps whose combined estimated duration exceeds the gap between
    #   their scheduled starts -> soft sweep_conflicts warning (not a hard error)
    tight = load_program("""
schema_version: 1
type: single
sweeps:
  - name: fast-a
    initial_startdate_pantilt: '2025-01-01 00:00:00'
    repeat_minutes: 1
    points: [{pan_deg: 0, tilt_deg: 0}, {pan_deg: 5, tilt_deg: 0}]
  - name: fast-b
    initial_startdate_pantilt: '2025-01-01 00:00:30'
    repeat_minutes: 1
    points: [{pan_deg: 0, tilt_deg: 0}, {pan_deg: 5, tilt_deg: 0}]
""")
    tight_summary = summarize(tight, PANTILT_CFG)
    assert len(tight_summary["sweep_conflicts"]) == 1
    tight_result = preview(dump_program(tight), PANTILT_CFG)
    assert tight_result["valid"]   # soft warning only -- doesn't block the run


if __name__ == "__main__":
    test_load_program()
    test_relative_limits()
    test_validate_points()
    test_pair_min_gap()
    test_validate_schedule()
    test_next_occurrence()
    test_summarize_and_preview()
    test_recorded_setup()
    test_repeat_minutes()
    test_sweeps()
    print("✅ All pantilt program tests passed")
