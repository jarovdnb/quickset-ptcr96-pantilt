#   Loading, validation and scheduling of pan-tilt measurement programs.
#   Shared by the dashboard (preview/run routes) and pantilt.py (execution),
#   so both always agree on what is valid and when a point is due.
#
#   Program file (YAML, uploaded through the dashboard):
#       schema_version: 1
#       name: winter-scan          # optional, used for the saved filename
#       type: single               # or: automated
#       initial_startdate_pantilt: '2025-04-07 12:00:00'   # automated, or single+repeat_minutes (UTC)
#       repeat_minutes: 30         # single only, optional: re-run the whole point
#                                  # sequence on a grid anchored at
#                                  # initial_startdate_pantilt, instead of running
#                                  # it once and stopping. Points run back-to-back
#                                  # within a sweep (no per-point wait) same as a
#                                  # plain single series; only the whole sweep repeats.
#       defaults:
#         try: 3                   # move/measure attempts per point
#         home_pan_abs: 10.0       # optional: recorded setup this program expects.
#         home_tilt_abs: -5.0      # Running it re-applies these to the pan-tilt
#         pan_orientation: 1       # app's actual config (warn first if they differ),
#         tilt_orientation: -1     # so the exact conditions can be reproduced later.
#                                  # orientation is 1 (normal) or -1 (inverted).
#                                  # Omit any/all of these four and they're simply
#                                  # backfilled from the current setup on first run.
#       points:
#         - pan_deg: 15.0          # relative to the home position
#           tilt_deg: 0.0
#           repeated_minutes: 30   # automated only
#           offset_sec: 120        # automated only
#
#   Automated schedule: each point fires on a fixed grid anchored at
#   initial_startdate_pantilt (not the Unix epoch): due times are
#   initial_startdate_pantilt + offset_sec + k * repeated_minutes*60. The grid
#   is deterministic, so it survives reboots and daemon restarts.
#
#   Single + repeat_minutes: the whole sweep (all points, back-to-back) is
#   itself treated as one slot on a grid anchored at initial_startdate_pantilt,
#   analogous to a single automated point with offset_sec=0. pantilt.py runs
#   the sweep, then idles until the next grid slot before starting over.
#
#   Multiple independently-scheduled sweeps (type: single only): replace the
#   top-level points:/repeat_minutes:/initial_startdate_pantilt: with a
#   sweeps: list, each entry its own named sweep with its own grid:
#       type: single
#       sweeps:
#         - name: hourly-tilt-sweep
#           initial_startdate_pantilt: '2026-08-21 00:00:00'
#           repeat_minutes: 60
#           points:
#             - {pan_deg: 0.0, tilt_deg: -30.0}
#         - name: daily-full-scan
#           initial_startdate_pantilt: '2026-08-21 03:00:00'
#           repeat_minutes: 1440
#           points:
#             - {pan_deg: -90.0, tilt_deg: 0.0}
#   Only one sweep runs at a time (one physical positioner): pantilt.py runs
#   whichever sweep is earliest due, ties broken by list order, and never
#   skips a sweep for lateness (a missed occurrence just runs once caught
#   up). Each sweep's grid is independent and anchored at its own
#   initial_startdate_pantilt, same deterministic-grid idea as above.
#   defaults: stays program-wide, shared by every sweep.

import math
import time
from datetime import date, datetime, timezone

import yaml

#   Rough duration of one VV+VH sweep, only for UI estimates
MEASURE_SECONDS_ESTIMATE = 35


class ProgramError(Exception):
    pass


def _parse_start_date(raw_start, context):
    #   YAML auto-parses an unquoted 'YYYY-MM-DD HH:MM:SS' into a native
    #   datetime; a quoted string is also accepted and parsed the same way
    if isinstance(raw_start, datetime):
        start_dt = raw_start
    elif isinstance(raw_start, date):
        start_dt = datetime(raw_start.year, raw_start.month, raw_start.day)
    elif isinstance(raw_start, str):
        try:
            start_dt = datetime.strptime(raw_start.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise ProgramError(f"initial_startdate_pantilt must be in the format 'YYYY-MM-DD HH:MM:SS' (UTC), {context}")
    else:
        raise ProgramError(f"initial_startdate_pantilt is required for {context} "
                            "(format 'YYYY-MM-DD HH:MM:SS', UTC)")

    return start_dt.replace(tzinfo=timezone.utc)


def load_program(text):
    #   Parse and structurally validate a program file; raises ProgramError
    try:
        prog = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ProgramError(f"Invalid YAML: {e}")

    if not isinstance(prog, dict):
        raise ProgramError("Program file must be a YAML mapping")

    if prog.get("schema_version") != 1:
        raise ProgramError("schema_version must be 1")

    prog_type = prog.get("type")
    if prog_type not in ("single", "automated"):
        raise ProgramError("type must be 'single' or 'automated'")

    repeat_minutes = None
    if prog_type == "single" and prog.get("repeat_minutes") is not None:
        repeat_minutes = prog.get("repeat_minutes")
        if not isinstance(repeat_minutes, int) or repeat_minutes < 1:
            raise ProgramError("repeat_minutes must be an integer >= 1")

    defaults = prog.get("defaults") or {}
    tries = defaults.get("try", 1)
    if not isinstance(tries, int) or tries < 1:
        raise ProgramError("defaults.try must be an integer >= 1")

    #   All four are optional: a recorded setup this program was written
    #   for/against. Present only if the file declares (or a prior run
    #   backfilled) them.
    parsed_defaults = {"try": tries}
    for key in ("home_pan_abs", "home_tilt_abs"):
        if key in defaults:
            if not isinstance(defaults[key], (int, float)):
                raise ProgramError(f"defaults.{key} must be a number")
            parsed_defaults[key] = float(defaults[key])
    for key in ("pan_orientation", "tilt_orientation"):
        if key in defaults:
            if defaults[key] not in (1, -1):
                raise ProgramError(f"defaults.{key} must be 1 or -1")
            parsed_defaults[key] = defaults[key]

    raw_sweeps = prog.get("sweeps")
    if raw_sweeps is not None:
        if prog_type != "single":
            raise ProgramError("'sweeps' is only valid for type: single")
        if prog.get("points") is not None:
            raise ProgramError("Program must declare either 'points' or 'sweeps', not both")
        if repeat_minutes is not None or prog.get("initial_startdate_pantilt") is not None:
            raise ProgramError("'repeat_minutes'/'initial_startdate_pantilt' must be set per-sweep, "
                                "not at top level, when 'sweeps' is used")
        if not isinstance(raw_sweeps, list) or len(raw_sweeps) == 0:
            raise ProgramError("'sweeps' must be a non-empty list")

        parsed_sweeps = []
        seen_names = set()
        for s_i, raw_sweep in enumerate(raw_sweeps):
            if not isinstance(raw_sweep, dict):
                raise ProgramError(f"Sweep {s_i + 1} must be a mapping")
            name = str(raw_sweep.get("name") or f"sweep{s_i + 1}")
            if name in seen_names:
                raise ProgramError(f"Sweep name '{name}' is used more than once")
            seen_names.add(name)

            sweep_repeat = raw_sweep.get("repeat_minutes")
            if not isinstance(sweep_repeat, int) or sweep_repeat < 1:
                raise ProgramError(f"Sweep '{name}': repeat_minutes must be an integer >= 1")

            start_dt = _parse_start_date(raw_sweep.get("initial_startdate_pantilt"), f"sweep '{name}'")

            sweep_points = raw_sweep.get("points")
            if not isinstance(sweep_points, list) or len(sweep_points) == 0:
                raise ProgramError(f"Sweep '{name}': points must be a non-empty list")
            for i, point in enumerate(sweep_points):
                if not isinstance(point, dict):
                    raise ProgramError(f"Sweep '{name}', point {i + 1} must be a mapping")
                for key in ("pan_deg", "tilt_deg"):
                    if not isinstance(point.get(key), (int, float)):
                        raise ProgramError(f"Sweep '{name}', point {i + 1}: '{key}' must be a number")

            parsed_sweeps.append({
                "name": name, "repeat_minutes": sweep_repeat,
                "initial_startdate_pantilt": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "initial_startdate_epoch": int(start_dt.timestamp()),
                "points": sweep_points,
            })

        prog["sweeps"] = parsed_sweeps

    else:
        points = prog.get("points")
        if not isinstance(points, list) or len(points) == 0:
            raise ProgramError("points must be a non-empty list")

        for i, point in enumerate(points):
            if not isinstance(point, dict):
                raise ProgramError(f"Point {i + 1} must be a mapping")

            for key in ("pan_deg", "tilt_deg"):
                if not isinstance(point.get(key), (int, float)):
                    raise ProgramError(f"Point {i + 1}: '{key}' must be a number")

            if prog_type == "automated":
                minutes = point.get("repeated_minutes")
                if not isinstance(minutes, int) or minutes < 1:
                    raise ProgramError(f"Point {i + 1}: 'repeated_minutes' must be an integer >= 1")

                offset = point.get("offset_sec", 0)
                if not isinstance(offset, int) or not 0 <= offset < minutes * 60:
                    raise ProgramError(f"Point {i + 1}: 'offset_sec' must be an integer in [0, repeated_minutes*60)")

        if prog_type == "automated" or repeat_minutes is not None:
            context = "automated programs and for single programs with repeat_minutes set"
            start_dt = _parse_start_date(prog.get("initial_startdate_pantilt"), context)
            prog["initial_startdate_pantilt"] = start_dt.strftime("%Y-%m-%d %H:%M:%S")
            prog["initial_startdate_epoch"] = int(start_dt.timestamp())

    prog["repeat_minutes"] = repeat_minutes
    prog["defaults"] = parsed_defaults
    prog["name"] = str(prog.get("name", "program"))
    return prog


def home_orientation_warnings(prog, pantilt_cfg):
    #   Only warns about fields the program actually declares; running it will
    #   overwrite the current home position / orientation to match
    d = prog["defaults"]
    warnings = []

    if "home_pan_abs" in d and float(pantilt_cfg.get("home_pan_abs", 0.0)) != d["home_pan_abs"]:
        warnings.append(f"Home pan will change from {pantilt_cfg.get('home_pan_abs', 0.0)}° to {d['home_pan_abs']}°")
    if "home_tilt_abs" in d and float(pantilt_cfg.get("home_tilt_abs", 0.0)) != d["home_tilt_abs"]:
        warnings.append(f"Home tilt will change from {pantilt_cfg.get('home_tilt_abs', 0.0)}° to {d['home_tilt_abs']}°")

    if "pan_orientation" in d:
        current = -1 if pantilt_cfg.get("pan_invert", 0) == 1 else 1
        if current != d["pan_orientation"]:
            warnings.append(f"Pan orientation will change from {current} to {d['pan_orientation']}")
    if "tilt_orientation" in d:
        current = -1 if pantilt_cfg.get("tilt_invert", 0) == 1 else 1
        if current != d["tilt_orientation"]:
            warnings.append(f"Tilt orientation will change from {current} to {d['tilt_orientation']}")

    return warnings


def home_orientation_overrides(prog, pantilt_cfg):
    #   The pan-tilt config fields to write so the live setup matches what this
    #   program was recorded against (only the keys it actually declares)
    d = prog["defaults"]
    overrides = {}
    if "home_pan_abs" in d:
        overrides["home_pan_abs"] = d["home_pan_abs"]
    if "home_tilt_abs" in d:
        overrides["home_tilt_abs"] = d["home_tilt_abs"]
    if "pan_orientation" in d:
        overrides["pan_invert"] = 1 if d["pan_orientation"] == -1 else 0
    if "tilt_orientation" in d:
        overrides["tilt_invert"] = 1 if d["tilt_orientation"] == -1 else 0
    return overrides


def backfill_recorded_setup(prog, pantilt_cfg):
    #   Called only when actually running a program: fills in whichever of the
    #   four setup fields the file didn't already specify, from the current
    #   config, so a later re-run reproduces the exact conditions this run
    #   happened under. Never overwrites a value the file already declared.
    d = prog["defaults"]
    if "home_pan_abs" not in d:
        d["home_pan_abs"] = float(pantilt_cfg.get("home_pan_abs", 0.0))
    if "home_tilt_abs" not in d:
        d["home_tilt_abs"] = float(pantilt_cfg.get("home_tilt_abs", 0.0))
    if "pan_orientation" not in d:
        d["pan_orientation"] = -1 if pantilt_cfg.get("pan_invert", 0) == 1 else 1
    if "tilt_orientation" not in d:
        d["tilt_orientation"] = -1 if pantilt_cfg.get("tilt_invert", 0) == 1 else 1

    #   Everything else: pure documentation of what was in effect, never
    #   validated or warned about on a later run
    prog["recorded_settings"] = {
        "pan_min_abs": float(pantilt_cfg.get("pan_min_abs", -180.0)),
        "pan_max_abs": float(pantilt_cfg.get("pan_max_abs", 180.0)),
        "tilt_min_abs": float(pantilt_cfg.get("tilt_min_abs", -90.0)),
        "tilt_max_abs": float(pantilt_cfg.get("tilt_max_abs", 90.0)),
        "pan_max_speed": int(pantilt_cfg.get("pan_max_speed", 64)),
        "tilt_max_speed": int(pantilt_cfg.get("tilt_max_speed", 64)),
        "settle_seconds": float(pantilt_cfg.get("settle_seconds", 2)),
        "move_timeout_seconds": float(pantilt_cfg.get("move_timeout_seconds", 120)),
        "measure_timeout_seconds": float(pantilt_cfg.get("measure_timeout_seconds", 400)),
        "min_gap_seconds": float(pantilt_cfg.get("min_gap_seconds", 60)),
        "heater_config": int(pantilt_cfg.get("heater_config", 1)),
    }


def dump_program(prog):
    #   Serialize back to YAML for saving; drop internal/derived-only fields
    #   and a repeat_minutes that's absent (None) rather than write it as null
    clean = {k: v for k, v in prog.items()
             if k != "initial_startdate_epoch" and not (k == "repeat_minutes" and v is None)}
    if clean.get("sweeps") is not None:
        clean["sweeps"] = [{k: v for k, v in sweep.items() if k != "initial_startdate_epoch"}
                            for sweep in clean["sweeps"]]
    return yaml.safe_dump(clean, default_flow_style=False, sort_keys=False)


def relative_limits(pantilt_cfg):
    #   pan_min_abs/pan_max_abs are the RAW (hardware) travel range — a
    #   physical fact, unaffected by the pan/tilt_invert software convention.
    #   Convert to the logical frame (pan_abs/tilt_abs, same as points and
    #   home position) first — inversion negates AND swaps min/max — then
    #   shift by the home position to get the reachable relative range.
    home_pan = float(pantilt_cfg.get("home_pan_abs", 0.0))
    home_tilt = float(pantilt_cfg.get("home_tilt_abs", 0.0))

    raw_pan_min = float(pantilt_cfg.get("pan_min_abs", -180.0))
    raw_pan_max = float(pantilt_cfg.get("pan_max_abs", 180.0))
    raw_tilt_min = float(pantilt_cfg.get("tilt_min_abs", -90.0))
    raw_tilt_max = float(pantilt_cfg.get("tilt_max_abs", 90.0))

    pan_min, pan_max = (-raw_pan_max, -raw_pan_min) if pantilt_cfg.get("pan_invert", 0) == 1 else (raw_pan_min, raw_pan_max)
    tilt_min, tilt_max = (-raw_tilt_max, -raw_tilt_min) if pantilt_cfg.get("tilt_invert", 0) == 1 else (raw_tilt_min, raw_tilt_max)

    return {
        "pan_min": pan_min - home_pan,
        "pan_max": pan_max - home_pan,
        "tilt_min": tilt_min - home_tilt,
        "tilt_max": tilt_max - home_tilt,
    }


def _point_report(i, point, limits, margin):
    #   Per point: 'error' outside the relative limits, 'warn' within
    #   warn_margin_deg of a limit, 'ok' otherwise
    pan = float(point["pan_deg"])
    tilt = float(point["tilt_deg"])
    status = "ok"
    messages = []

    for name, value, low, high in (("pan", pan, limits["pan_min"], limits["pan_max"]),
                                    ("tilt", tilt, limits["tilt_min"], limits["tilt_max"])):
        if not low <= value <= high:
            status = "error"
            messages.append(f"{name} {value}° outside relative limits [{low}°, {high}°]")
        elif value < low + margin or value > high - margin:
            if status != "error":
                status = "warn"
            messages.append(f"{name} {value}° within {margin}° of a limit")

    return {"index": i, "pan_deg": pan, "tilt_deg": tilt, "status": status, "message": "; ".join(messages)}


def validate_points(prog, pantilt_cfg):
    limits = relative_limits(pantilt_cfg)
    margin = float(pantilt_cfg.get("warn_margin_deg", 2.0))
    reports = []

    sweeps = prog.get("sweeps")
    if sweeps is not None:
        for s_i, sweep in enumerate(sweeps):
            for i, point in enumerate(sweep["points"]):
                report = _point_report(i, point, limits, margin)
                report["sweep"] = s_i
                report["sweep_name"] = sweep["name"]
                reports.append(report)
        return reports

    for i, point in enumerate(prog["points"]):
        report = _point_report(i, point, limits, margin)
        if prog["type"] == "automated":
            report["repeated_minutes"] = point["repeated_minutes"]
            report["offset_sec"] = point.get("offset_sec", 0)
        reports.append(report)

    return reports


def pair_min_gap(period1_s, offset1_s, period2_s, offset2_s):
    #   Minimal time between any two occurrences of two periodic schedules.
    #   The set of pairwise differences is (offset1 - offset2) + gcd(p1, p2) * Z,
    #   so the minimum distance is min(d, g - d) with d = (o1 - o2) mod g.
    g = math.gcd(period1_s, period2_s)
    d = (offset1_s - offset2_s) % g
    return min(d, g - d)


def validate_schedule(prog, pantilt_cfg):
    #   Automated only: no two measurements may be scheduled closer together
    #   than min_gap_seconds. Exact over the infinite horizon (gcd test).
    if prog["type"] != "automated":
        return []

    min_gap = int(pantilt_cfg.get("min_gap_seconds", 60))
    points = prog["points"]
    conflicts = []

    for i in range(len(points)):
        p_i = points[i]["repeated_minutes"] * 60
        o_i = points[i].get("offset_sec", 0)

        if p_i < min_gap:
            conflicts.append({"points": [i], "gap_seconds": p_i,
                              "message": f"Point {i + 1} repeats every {p_i} s, closer than the minimum gap of {min_gap} s"})

        for j in range(i + 1, len(points)):
            p_j = points[j]["repeated_minutes"] * 60
            o_j = points[j].get("offset_sec", 0)
            gap = pair_min_gap(p_i, o_i, p_j, o_j)
            if gap < min_gap:
                conflicts.append({"points": [i, j], "gap_seconds": gap,
                                  "message": f"Points {i + 1} and {j + 1} come within {gap} s of each other (minimum gap {min_gap} s)"})

    return conflicts


def next_occurrence(point, now_s, ref_epoch=0):
    #   First due time strictly after now_s (unix seconds), on the point's
    #   grid anchored at ref_epoch (the program's initial_startdate_pantilt)
    period = point["repeated_minutes"] * 60
    anchor = ref_epoch + point.get("offset_sec", 0)
    return now_s - ((now_s - anchor) % period) + period


def upcoming(prog, now_s, horizon_s=86400, limit=50):
    #   Occurrence timeline for the preview (display only; execution uses
    #   next_occurrence directly)
    events = []
    ref_epoch = prog.get("initial_startdate_epoch", 0)
    for i, point in enumerate(prog["points"]):
        due = next_occurrence(point, now_s, ref_epoch)
        while due <= now_s + horizon_s:
            events.append((due, i))
            due += point["repeated_minutes"] * 60

    events.sort()
    return [{"utc": datetime.fromtimestamp(due, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             "point": i} for due, i in events[:limit]]


def upcoming_sweeps(prog, now_s, horizon_s=86400, limit=50):
    #   Same as upcoming(), but one grid per sweep instead of per point
    events = []
    for i, sweep in enumerate(prog["sweeps"]):
        due = next_occurrence({"repeated_minutes": sweep["repeat_minutes"], "offset_sec": 0},
                               now_s, sweep["initial_startdate_epoch"])
        while due <= now_s + horizon_s:
            events.append((due, i))
            due += sweep["repeat_minutes"] * 60

    events.sort()
    return [{"utc": datetime.fromtimestamp(due, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             "sweep": i, "sweep_name": prog["sweeps"][i]["name"]} for due, i in events[:limit]]


def _sweep_duration_seconds(points, speed, settle):
    #   Estimated wall-clock time to run a sequence of points back-to-back
    #   starting from the home position
    duration = 0.0
    pan, tilt = 0.0, 0.0
    for point in points:
        duration += max(abs(point["pan_deg"] - pan), abs(point["tilt_deg"] - tilt)) / speed
        duration += settle + MEASURE_SECONDS_ESTIMATE
        pan, tilt = point["pan_deg"], point["tilt_deg"]
    return duration


def _sweep_overlap_warnings(sweeps, sweep_summaries):
    #   Soft, preview-only heads-up (not a hard conflict): if two sweeps'
    #   combined estimated duration exceeds the minimum gap between their
    #   scheduled start times, one may start late and queue behind the
    #   other. Duration is only an estimate (retries/timeouts vary it), and
    #   the daemon queues rather than errors on a real overlap, so this
    #   never blocks a run the way validate_schedule()'s exact automated
    #   check does.
    warnings = []
    for i in range(len(sweeps)):
        for j in range(i + 1, len(sweeps)):
            gap = pair_min_gap(sweeps[i]["repeat_minutes"] * 60, sweeps[i]["initial_startdate_epoch"],
                                sweeps[j]["repeat_minutes"] * 60, sweeps[j]["initial_startdate_epoch"])
            threshold = sweep_summaries[i]["estimated_minutes"] * 60 + sweep_summaries[j]["estimated_minutes"] * 60
            if gap < threshold:
                warnings.append({"sweeps": [i, j],
                                  "message": f"Sweeps '{sweeps[i]['name']}' and '{sweeps[j]['name']}' can come "
                                             f"within {int(gap)} s of each other's scheduled start — their "
                                             f"combined estimated duration is {int(threshold)} s, so one may "
                                             f"start late and queue behind the other"})
    return warnings


def summarize(prog, pantilt_cfg):
    #   Short overview shown in the dashboard preview
    speed = max(float(pantilt_cfg.get("speed_deg_per_s_estimate", 4.0)), 0.1)
    settle = float(pantilt_cfg.get("settle_seconds", 2))

    sweeps = prog.get("sweeps")
    if sweeps is not None:
        summary = {"name": prog["name"], "type": prog["type"], "n_sweeps": len(sweeps),
                   "n_points": sum(len(s["points"]) for s in sweeps), "tries": prog["defaults"]["try"],
                   "sweeps": []}
        for sweep in sweeps:
            duration = _sweep_duration_seconds(sweep["points"], speed, settle)
            sweep_summary = {"name": sweep["name"], "n_points": len(sweep["points"]),
                              "estimated_minutes": round(duration / 60, 1),
                              "repeat_minutes": sweep["repeat_minutes"],
                              "initial_startdate_pantilt": sweep["initial_startdate_pantilt"]}
            if duration / 60 > sweep["repeat_minutes"]:
                sweep_summary.setdefault("warnings", []).append(
                    f"Estimated sweep time ({round(duration / 60, 1)} min) exceeds this sweep's repeat_minutes "
                    f"({sweep['repeat_minutes']} min) — it would overlap/back up against itself")
            summary["sweeps"].append(sweep_summary)
        summary["sweep_conflicts"] = _sweep_overlap_warnings(sweeps, summary["sweeps"])
    else:
        points = prog["points"]
        summary = {"name": prog["name"], "type": prog["type"], "n_points": len(points),
                   "tries": prog["defaults"]["try"]}

        if prog["type"] == "single":
            #   Sequential run starting from the home position
            duration = _sweep_duration_seconds(points, speed, settle)
            summary["estimated_minutes"] = round(duration / 60, 1)
            if prog.get("repeat_minutes"):
                summary["repeat_minutes"] = prog["repeat_minutes"]
                summary["initial_startdate_pantilt"] = prog["initial_startdate_pantilt"]
                if duration / 60 > prog["repeat_minutes"]:
                    summary.setdefault("warnings", []).append(
                        f"Estimated sweep time ({round(duration / 60, 1)} min) exceeds repeat_minutes "
                        f"({prog['repeat_minutes']} min) — sweeps would overlap/back up")
        else:
            summary["measurements_per_hour"] = round(sum(3600 / (p["repeated_minutes"] * 60) for p in points), 1)
            summary["initial_startdate_pantilt"] = prog["initial_startdate_pantilt"]

    #   Recorded setup (if any), shown the same way for single and automated
    for key in ("home_pan_abs", "home_tilt_abs", "pan_orientation", "tilt_orientation"):
        if key in prog["defaults"]:
            summary[key] = prog["defaults"][key]

    return summary


def preview(text, pantilt_cfg):
    #   Everything the dashboard needs to render the program overview
    prog = load_program(text)
    points = validate_points(prog, pantilt_cfg)
    conflicts = validate_schedule(prog, pantilt_cfg)

    result = {"summary": summarize(prog, pantilt_cfg),
              "points": points,
              "conflicts": conflicts,
              "home_warnings": home_orientation_warnings(prog, pantilt_cfg),
              "valid": all(p["status"] != "error" for p in points) and not conflicts,
              "relative_limits": relative_limits(pantilt_cfg)}

    if prog["type"] == "automated":
        result["timeline"] = upcoming(prog, int(time.time()), horizon_s=86400, limit=25)
    elif prog.get("sweeps") is not None:
        result["timeline"] = upcoming_sweeps(prog, int(time.time()), horizon_s=86400, limit=25)

    return result
