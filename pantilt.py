#   Pan-tilt daemon (MOOG QuickSet QPT-90 / PTCR-96 controller, MN00162 Rev C)
#
#   Long-running service: polls its OWN config file (~/pantilt_config.yaml)
#   every 100 ms for commands from app_pantilt.py, owns the serial/IP link
#   to the positioner, executes measurement programs and broadcasts live
#   status on a unix socket for the dashboard.
#
#   Measurements are triggered through lib.measurement.take_measurement() --
#   see that module's docstring, it's the one place meant to be edited to
#   hook this scheduler up to your own instrument.
#
#   Crash recovery: home position, limits and program progress live in this
#   app's own config file (pantilt / pantilt_status sections). The positioner
#   has absolute position feedback, so after a power cycle no homing is
#   needed and an interrupted program simply continues.

import json
import os
import threading
import time
from datetime import datetime, timezone

from lib.configuration import retrieve_yaml_file, update_yaml_flag, update_yaml_flags, ensure_yaml_section
from lib.socket_helper import bind_local_socket, close_local_socket
from lib import measurement
from lib import qpt90
from lib import pantilt_program
from lib import keepout

#   Hard caps (safety limits, not a PTCR-96 protocol constraint -- some
#   PTCR-96 platforms support continuous pan rotation): the config limits
#   can never exceed these
PAN_ABS_CAP = 180.0
TILT_ABS_CAP = 90.0

PANTILT_SOCKET_PATH = "/tmp/pantilt_socket.sock"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
#   One keep-out sample file per instrument/mount (different antenna geometry
#   -> different safe envelope); which one is active is the keepout_profile
#   setting below. pt_keepout_record.py writes into this same directory.
KEEPOUT_DIR = os.path.join(BASE_DIR, "config", "keepout")
os.makedirs(KEEPOUT_DIR, exist_ok=True)

#   Connection retry ladder: every 10 s for the first 5 minutes, then every 30 minutes
RETRY_FAST_S = 10
RETRY_FAST_WINDOW_S = 300
RETRY_SLOW_S = 1800

#   Defaults, also used to migrate an already-deployed pan-tilt config file
PANTILT_DEFAULTS = {
    "enabled": 0, "port": "", "baud": 9600,
    #   IP transport (lib/qpt90.py's open_tcp()/find_qpt90_tcp()), an
    #   alternative to the port/baud serial link above -- this unit's
    #   Lantronix "IP option", a serial-to-Ethernet bridge wired to the same
    #   RS-232 port. transport="serial" (default) uses port/baud exactly as
    #   before; transport="ip" uses host/tcp_port instead. See try_connect().
    "transport": "serial", "host": "", "tcp_port": 10001,
    "pan_min_abs": -180.0, "pan_max_abs": 180.0,
    "tilt_min_abs": -90.0, "tilt_max_abs": 90.0,
    "home_pan_abs": 0.0, "home_tilt_abs": 0.0,
    "pan_invert": 0, "tilt_invert": 0,
    "pan_max_speed": 64, "tilt_max_speed": 64,
    "speed_deg_per_s_estimate": 4.0, "settle_seconds": 2,
    "move_timeout_seconds": 120, "measure_timeout_seconds": 400,
    "min_gap_seconds": 60, "warn_margin_deg": 2.0, "update": 0,
    "step_pan": 0.0, "step_tilt": 0.0, "step_request": 0,
    "move_pan_rel": 0.0, "move_tilt_rel": 0.0, "move_request": 0,
    "set_home": 0, "clear_fault": 0, "measure_request": 0,
    "run_program": "", "pause_program": 0, "resume_program": 0, "stop_program": 0,
    "heater_config": 1,   # 1=off
    "keepout_profile": "",   # basename (no .json) of the active file in config/keepout/; "" = unrestricted
}
PANTILT_STATUS_DEFAULTS = {
    "connected": 0, "pan_rel": 0.0, "tilt_rel": 0.0, "pan_abs": 0.0, "tilt_abs": 0.0,
    "active_program": "", "program_type": "", "program_next_index": 0,
    "program_paused": 0, "fault": "", "heater_state": 0,
    "program_active_sweep": -1,  # index into active_prog["sweeps"] currently mid-execution, -1 = idle/not a sweeps program
}

#   Daemon state
driver = None                # lib.qpt90.Qpt90 instance while connected
serial_port = None           # the underlying pyserial port
connected_port_name = ""
retry_started = 0.0          # start of the current disconnected period
next_retry = 0.0

active_prog = None           # parsed program (cache of pantilt_status.active_program)
active_prog_file = ""
auto_schedule = {}           # point index -> next due time (unix s), automated only
series_next_due = None       # unix s the next sweep may start, single+repeat_minutes only
sweep_schedule = {}          # sweep index -> next due time (unix s), sweeps only

#   Coupled pan/tilt keep-out envelope (lib/keepout.py) for whichever
#   instrument/mount is currently selected (pantilt_cfg.keepout_profile).
#   Empty list = no profile selected, or nothing measured yet = unrestricted
#   (keepout.is_safe always returns True).
_keepout_breakpoints = []
_keepout_profile_loaded = None   # sentinel: None means "never loaded yet"

#   Live state broadcast on the pantilt socket (written by the main loop only)
live = {
    "pantilt_enabled": 0, "connected": 0, "connecting": 0, "port": "",
    "pan_rel": 0.0, "tilt_rel": 0.0, "pan_abs": 0.0, "tilt_abs": 0.0,
    "moving": 0, "measuring": 0, "fault": "", "retry_in_s": 0,
    "program_name": "", "program_type": "", "program_state": "",
    "program_progress": "", "next_measurement_utc": "", "next_measurement_in_s": 0,
    "next_pan_rel": 0.0, "next_tilt_rel": 0.0,
    "steps_done": 0, "steps_total": 0, "upcoming_points": [], "estimated_remaining_s": 0,
    "heater_state": 0, "keepout_profile": "", "keepout_breakpoints": 0,
}


class ConnectionLost(Exception):
    pass


# *** *** #
def stream_data(conn):
    with conn:
        try:
            conn.sendall((json.dumps(live) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError) as e:
            print(f"⚠️ Broken connection during transmission: {e}")
        except Exception as e:
            print(f"❌ Unexpected error during transmission: {e}")

def start_server():
    server = bind_local_socket(PANTILT_SOCKET_PATH)
    server.listen(1)

    print(f"🟢 Pan-tilt server luistert op {PANTILT_SOCKET_PATH}")

    try:
        while True:
            conn, _ = server.accept()
            t = threading.Thread(target=stream_data, args=(conn,))
            t.daemon = True
            t.start()
    except KeyboardInterrupt:
        print("🛑 Pan-tilt server stopped.")
    finally:
        close_local_socket(server, PANTILT_SOCKET_PATH)
# *** *** #


#   Limits & fault helpers

def effective_limits(pantilt_cfg):
    #   pan_min_abs/pan_max_abs describe the RAW (hardware) travel range — a
    #   physical fact that doesn't change just because pan/tilt_invert flips
    #   the software sign convention. Clip to the hard caps in that raw frame,
    #   then convert to the logical frame (pan_abs/tilt_abs) everything else
    #   is validated against: inversion negates AND swaps min/max.
    raw_pan_min = max(float(pantilt_cfg.get("pan_min_abs", -PAN_ABS_CAP)), -PAN_ABS_CAP)
    raw_pan_max = min(float(pantilt_cfg.get("pan_max_abs", PAN_ABS_CAP)), PAN_ABS_CAP)
    raw_tilt_min = max(float(pantilt_cfg.get("tilt_min_abs", -TILT_ABS_CAP)), -TILT_ABS_CAP)
    raw_tilt_max = min(float(pantilt_cfg.get("tilt_max_abs", TILT_ABS_CAP)), TILT_ABS_CAP)

    pan_min, pan_max = (-raw_pan_max, -raw_pan_min) if pantilt_cfg.get("pan_invert", 0) == 1 else (raw_pan_min, raw_pan_max)
    tilt_min, tilt_max = (-raw_tilt_max, -raw_tilt_min) if pantilt_cfg.get("tilt_invert", 0) == 1 else (raw_tilt_min, raw_tilt_max)

    return (pan_min, pan_max, tilt_min, tilt_max)


def target_allowed(pan_abs, tilt_abs, pantilt_cfg):
    pan_min, pan_max, tilt_min, tilt_max = effective_limits(pantilt_cfg)
    return pan_min <= pan_abs <= pan_max and tilt_min <= tilt_abs <= tilt_max


def set_fault(text):
    if live["fault"] != text:
        print(f"⚠️ Pan-tilt fault: {text}" if text else "✅ Pan-tilt fault cleared")
        live["fault"] = text
        update_yaml_flag("pantilt_status", "fault", text)


def load_keepout(pantilt_cfg):
    #   Re-run whenever keepout_profile changes (see process_commands's
    #   "update" branch) as well as at daemon start -- cheap (a small JSON
    #   file). pt_keepout_record.py writes samples offline (it can't hold the
    #   serial port at the same time as this daemon), so there's no need to
    #   hot-reload the SAME file mid-session.
    global _keepout_breakpoints, _keepout_profile_loaded
    profile = pantilt_cfg.get("keepout_profile", "")
    _keepout_profile_loaded = profile
    path = keepout.profile_path(KEEPOUT_DIR, profile)

    if not path:
        _keepout_breakpoints = []
        live["keepout_profile"] = ""
        live["keepout_breakpoints"] = 0
        print("ℹ️ No keep-out profile selected -- pan/tilt moves are unrestricted by antenna geometry")
        return

    _keepout_breakpoints = keepout.load_breakpoints(path)
    live["keepout_profile"] = profile
    live["keepout_breakpoints"] = len(_keepout_breakpoints)

    if _keepout_breakpoints:
        print(f"✅ Keep-out envelope loaded: profile '{profile}', {len(_keepout_breakpoints)} breakpoint(s)")
    else:
        print(f"⚠️ Keep-out profile '{profile}' has no/insufficient samples yet ({path}) -- "
              "pan/tilt moves are unrestricted by antenna geometry")


#   Connection handling

def flip(value, inverted):
    #   Sign convention between our "logical" pan/tilt (what home position,
    #   limits, programs and filenames are expressed in) and the QPT's own raw
    #   angle. Mounting the unit upside-down or reversed flips this mapping
    #   without touching the positioner's own calibration (same approach as
    #   fixed_configurations.polarisation_inverted in librevna.py).
    return -value if inverted else value


def poll_status(pantilt_cfg):
    #   Status request = keep-alive; also refreshes the live position.
    #   Raises ConnectionLost when the serial link is gone.
    try:
        status = driver.get_status()
    except (qpt90.QptError, OSError) as e:
        raise ConnectionLost(str(e))

    pan_deg = flip(status.pan_deg, pantilt_cfg.get("pan_invert", 0) == 1)
    tilt_deg = flip(status.tilt_deg, pantilt_cfg.get("tilt_invert", 0) == 1)

    home_pan = float(pantilt_cfg.get("home_pan_abs", 0.0))
    home_tilt = float(pantilt_cfg.get("home_tilt_abs", 0.0))

    live["pan_abs"] = round(pan_deg, 1)
    live["tilt_abs"] = round(tilt_deg, 1)
    live["pan_rel"] = round(pan_deg - home_pan, 1)
    live["tilt_rel"] = round(tilt_deg - home_tilt, 1)
    live["moving"] = 1 if status.moving else 0

    #   Latched positioner faults (TO/DE/OL) are surfaced, never auto-cleared
    if status.faults and not live["fault"]:
        set_fault("Positioner fault: " + ", ".join(status.faults))

    return status


def persist_position(pantilt_cfg):
    #   Written only when motion has settled (not at poll rate).
    update_yaml_flags("pantilt_status", {
        "pan_rel": live["pan_rel"], "tilt_rel": live["tilt_rel"],
        "pan_abs": live["pan_abs"], "tilt_abs": live["tilt_abs"],
    })


def apply_heater_config(pantilt_cfg):
    #   97H: set the desired mode, then re-query independently to confirm the
    #   unit actually accepted it (a mismatch means no heater is fitted, or a
    #   fault prevented the change) rather than trusting only the set's own ACK.
    #
    #   The UI/config heater_config field is 1=Off/2=Share/3=Full (unchanged
    #   convention, shared with the old removed driver's byte values), but
    #   qpt90's actual wire format (MN00162 Sec 2.9.7) is 0=No Heat/1=Share/
    #   2=Full Heat with a separate Query bit -- translate by -1/+1 at this
    #   boundary and keep the UI-facing 1/2/3 convention everywhere else.
    desired_ui = int(pantilt_cfg.get("heater_config", 1))
    desired = max(qpt90.HEATER_OFF, min(qpt90.HEATER_FULL, desired_ui - 1))
    try:
        driver.set_heater_config(desired)
        confirmed = driver.get_heater_config()
    except (qpt90.QptError, OSError) as e:
        print(f"⚠️ Heater config could not be applied: {e}")
        live["heater_state"] = 0
        update_yaml_flag("pantilt_status", "heater_state", 0)
        set_fault(f"Heater config could not be applied: {e}")
        return False

    confirmed_ui = confirmed + 1
    live["heater_state"] = confirmed_ui
    update_yaml_flag("pantilt_status", "heater_state", confirmed_ui)

    if confirmed != desired:
        set_fault(f"Heater confirmed in mode {confirmed_ui}, requested mode {desired_ui}")
        return False

    return True


def apply_comm_timeout():
    #   96H: hardware backstop -- the positioner halts itself if the daemon
    #   dies or the link drops for more than this many seconds. Fixed value,
    #   not user-configurable (matches the old removed driver's behavior).
    try:
        driver.set_comm_timeout(2)
    except (qpt90.QptError, OSError) as e:
        print(f"⚠️ Comm timeout could not be applied: {e}")
        set_fault(f"Comm timeout could not be applied: {e}")
        return False
    return True


def apply_max_speed(pantilt_cfg):
    #   9CH: caps automated-move speed per axis. Session-only (not written to
    #   non-volatile memory) so every reconnect re-applies whatever the
    #   config currently says, same as heater/comm-timeout.
    pan_max = int(pantilt_cfg.get("pan_max_speed", 64))
    tilt_max = int(pantilt_cfg.get("tilt_max_speed", 64))
    try:
        driver.set_max_speed(pan_max, tilt_max)
    except (qpt90.QptError, OSError) as e:
        print(f"⚠️ Max speed could not be applied: {e}")
        set_fault(f"Max speed could not be applied: {e}")
        return False
    return True


def apply_positioner_settings(pantilt_cfg):
    apply_comm_timeout()
    apply_max_speed(pantilt_cfg)
    apply_heater_config(pantilt_cfg)


def module_disabled():
    #   Polled by qpt90.find_qpt90/connect during a scan so disabling the module
    #   mid-connect aborts promptly instead of waiting out the full port scan
    #   (each candidate port can take up to ~20 s to time out).
    return retrieve_yaml_file().get("pantilt", {}).get("enabled", 0) != 1


def try_connect(config):
    global driver, serial_port, connected_port_name

    pantilt_cfg = config.get("pantilt", {})

    if pantilt_cfg.get("transport", "serial") == "ip":
        connected_port_name, driver = qpt90.find_qpt90_tcp(
            pantilt_cfg.get("host", ""), int(pantilt_cfg.get("tcp_port", 10001)),
            should_abort=module_disabled)
    else:
        connected_port_name, driver = qpt90.find_qpt90(
            pantilt_cfg.get("port", ""), int(pantilt_cfg.get("baud", 9600)),
            should_abort=module_disabled)
    serial_port = driver.ser if driver else None

    if driver is None:
        return False

    try:
        apply_positioner_settings(pantilt_cfg)
        poll_status(pantilt_cfg)
        persist_position(pantilt_cfg)
    except (ConnectionLost, qpt90.QptError, OSError) as e:
        print(f"⚠️ Pan-tilt connection lost during setup: {e}")
        disconnect()
        return False

    live["connected"] = 1
    live["port"] = connected_port_name
    live["retry_in_s"] = 0
    update_yaml_flag("pantilt_status", "connected", 1)
    print(f"✅ Pan-tilt connected on {connected_port_name}")
    return True


def disconnect(stop_motion=False):
    global driver, serial_port, connected_port_name

    if driver is not None and stop_motion:
        try:
            driver.stop()
        except Exception:
            pass
    if serial_port is not None:
        try:
            serial_port.close()
        except Exception:
            pass

    driver = None
    serial_port = None
    connected_port_name = ""
    live["connected"] = 0
    live["port"] = ""
    live["moving"] = 0
    live["heater_state"] = 0
    update_yaml_flag("pantilt_status", "connected", 0)
    update_yaml_flag("pantilt_status", "heater_state", 0)


#   Blocking motion / measurement primitives.
#   They keep polling the positioner (keep-alive) while waiting and abort when
#   the module is disabled. Pause/stop of a program take effect between points.

def wait_move_done(pantilt_cfg):
    #   Returns "done", "aborted" (module disabled) or "fault"
    deadline = time.time() + float(pantilt_cfg.get("move_timeout_seconds", 120))
    last_config_check = 0.0

    while time.time() < deadline:
        time.sleep(0.1)
        status = poll_status(pantilt_cfg)

        if status.faults:
            return "fault"
        if not status.moving:
            persist_position(pantilt_cfg)
            return "done"

        #   React to the module being switched off mid-move
        if time.time() - last_config_check > 1.0:
            last_config_check = time.time()
            config = retrieve_yaml_file()
            if config.get("pantilt", {}).get("enabled", 0) == 0:
                driver.stop()
                return "aborted"

    driver.stop()
    set_fault(f"Move timed out after {pantilt_cfg.get('move_timeout_seconds', 120)} s")
    return "fault"


def _send_move(raw_pan, raw_tilt, pan_abs, tilt_abs, pantilt_cfg):
    #   Sends ONE move to the driver and waits for it to finish. pan_abs/
    #   tilt_abs (logical/display values) are only used for the fault
    #   message if the positioner itself rejects the move.
    try:
        driver.move_to(raw_pan, raw_tilt)
    except qpt90.QptNak:
        set_fault(f"Positioner rejected move to pan {pan_abs}° / tilt {tilt_abs}°")
        return "fault"
    except (qpt90.QptError, OSError) as e:
        raise ConnectionLost(str(e))
    return wait_move_done(pantilt_cfg)


def move_abs(pan_abs, tilt_abs, pantilt_cfg):
    #   Validated absolute move; returns "done", "aborted" or "fault". Two
    #   independent safety checks: the box limits (pan/tilt min/max_abs) and
    #   the coupled antenna/bar keep-out envelope (lib/keepout.py) -- see
    #   that module's docstring for why the keep-out check operates on RAW
    #   hardware angles, not these logical ones.
    if not target_allowed(pan_abs, tilt_abs, pantilt_cfg):
        pan_min, pan_max, tilt_min, tilt_max = effective_limits(pantilt_cfg)
        set_fault(f"Move to pan {pan_abs}° / tilt {tilt_abs}° refused: outside absolute limits "
                  f"(pan [{pan_min}°, {pan_max}°], tilt [{tilt_min}°, {tilt_max}°])")
        return "fault"

    pan_inverted = pantilt_cfg.get("pan_invert", 0) == 1
    tilt_inverted = pantilt_cfg.get("tilt_invert", 0) == 1
    raw_pan = flip(pan_abs, pan_inverted)
    raw_tilt = flip(tilt_abs, tilt_inverted)

    if not keepout.is_safe(raw_pan, raw_tilt, _keepout_breakpoints):
        set_fault(f"Move to pan {pan_abs}° / tilt {tilt_abs}° refused: inside the antenna/bar keep-out zone")
        return "fault"

    if not _keepout_breakpoints:
        return _send_move(raw_pan, raw_tilt, pan_abs, tilt_abs, pantilt_cfg)

    cur_raw_pan = flip(live["pan_abs"], pan_inverted)
    cur_raw_tilt = flip(live["tilt_abs"], tilt_inverted)

    if cur_raw_pan == raw_pan:
        #   Pan fixed, tilt sweeping -- lib/keepout.py is pan-indexed, so a
        #   fixed pan has ONE contiguous safe tilt band; the endpoint check
        #   above already covers the whole path (both endpoints inside that
        #   same single interval implies everything between them is too).
        return _send_move(raw_pan, raw_tilt, pan_abs, tilt_abs, pantilt_cfg)

    if cur_raw_tilt == raw_tilt:
        #   Tilt fixed, pan sweeping -- NOT covered by the endpoint check:
        #   the safe tilt band can differ at every pan crossed (that's the
        #   whole reason for pan-indexing -- e.g. a bar of finite length,
        #   clear near both pan extremes but blocking a fixed mid tilt near
        #   pan=0). Both endpoints being individually safe does not mean the
        #   straight sweep between them stays out of the zone; verify the
        #   fixed tilt against the full pan range crossed, same as a
        #   diagonal move's tilt-side check below.
        safe_range = keepout.safe_tilt_intersection_over_sweep(cur_raw_pan, raw_pan, _keepout_breakpoints)
        if safe_range is None or not (safe_range[0] <= raw_tilt <= safe_range[1]):
            set_fault(f"Move to pan {pan_abs}° / tilt {tilt_abs}° refused: keep-out zone blocks "
                      f"a direct pan sweep at this tilt")
            return "fault"
        return _send_move(raw_pan, raw_tilt, pan_abs, tilt_abs, pantilt_cfg)

    #   Coupled move: the protocol doesn't guarantee a straight-line path
    #   between two diagonal pan/tilt targets -- each axis has its own motor
    #   and speed with no coordinated trajectory described in MN00162, so a
    #   single diagonal move_to could cut through the keep-out zone even if
    #   both endpoints are individually safe. Sequence it as three
    #   single-axis legs instead, through a tilt value proven safe across
    #   every pan the move will cross (lib/keepout.py is pan-indexed: a
    #   fixed pan has one contiguous safe tilt band for this mount).
    raw_tilt_min = max(float(pantilt_cfg.get("tilt_min_abs", -TILT_ABS_CAP)), -TILT_ABS_CAP)
    raw_tilt_max = min(float(pantilt_cfg.get("tilt_max_abs", TILT_ABS_CAP)), TILT_ABS_CAP)
    safe_range = keepout.safe_tilt_intersection_over_sweep(cur_raw_pan, raw_pan, _keepout_breakpoints)
    if safe_range is not None:
        lo = max(safe_range[0], raw_tilt_min)
        hi = min(safe_range[1], raw_tilt_max)
        safe_range = (lo, hi) if lo <= hi else None
    if safe_range is None:
        set_fault(f"Move to pan {pan_abs}° / tilt {tilt_abs}° refused: no single-axis-safe path "
                  f"avoids the keep-out zone between the current and target pan")
        return "fault"
    waypoint_tilt = min(max(cur_raw_tilt, safe_range[0]), safe_range[1])

    result = _send_move(cur_raw_pan, waypoint_tilt, pan_abs, tilt_abs, pantilt_cfg)
    if result != "done":
        return result
    result = _send_move(raw_pan, waypoint_tilt, pan_abs, tilt_abs, pantilt_cfg)
    if result != "done":
        return result
    return _send_move(raw_pan, raw_tilt, pan_abs, tilt_abs, pantilt_cfg)


def settle(pantilt_cfg):
    #   Dwell after a move so vibrations die out before measuring
    until = time.time() + float(pantilt_cfg.get("settle_seconds", 2))
    while time.time() < until:
        time.sleep(0.1)
        poll_status(pantilt_cfg)


def measure(pantilt_cfg):
    #   Trigger one measurement via lib/measurement.py's take_measurement() --
    #   the single customization point for hooking this scheduler up to your
    #   own instrument, see that module's docstring. Returns "done", "aborted"
    #   (module disabled mid-call) or "unreachable" (take_measurement()
    #   reported its instrument couldn't be reached this time). Deliberately
    #   NOT "fault": callers treat "unreachable" as "skip this point, keep
    #   the schedule going" rather than pausing for a human -- an actual
    #   positioner fault never reaches this function (run_point() returns
    #   early on those, before measure() is called).
    result = measurement.take_measurement(live["pan_rel"], live["tilt_rel"])

    if retrieve_yaml_file().get("pantilt", {}).get("enabled", 0) == 0:
        #   Module was disabled while take_measurement() was running
        return "aborted"

    if result == "done":
        if live["fault"]:
            #   A measurement just actually completed, so any "unreachable"
            #   fault left over from an earlier point is stale now.
            set_fault("")
        return "done"

    set_fault("Measurement instrument not reachable -- skipping this point")
    return "unreachable"


def run_point(point, pantilt_cfg, tries):
    #   Move to a program point and measure there, both with retries.
    #   Returns "done", "aborted", "fault" (move/positioner problem -- from
    #   move_abs, before measure() is ever reached) or "unreachable"
    #   (from measure(); see its docstring for why that's kept distinct).
    pan_abs = float(pantilt_cfg.get("home_pan_abs", 0.0)) + float(point["pan_deg"])
    tilt_abs = float(pantilt_cfg.get("home_tilt_abs", 0.0)) + float(point["tilt_deg"])

    for attempt in range(tries):
        if attempt > 0:
            #   A latched fault blocks motion until it is reset
            driver.clear_faults()
        result = move_abs(pan_abs, tilt_abs, pantilt_cfg)
        if result != "fault":
            break
        print(f"⚠️ Move attempt {attempt + 1}/{tries} failed")
    if result != "done":
        return result

    settle(pantilt_cfg)

    live["measuring"] = 1
    for attempt in range(tries):
        result = measure(pantilt_cfg)
        if result not in ("fault", "unreachable"):
            break
        print(f"⚠️ Measurement attempt {attempt + 1}/{tries} failed ({result})")
    live["measuring"] = 0
    return result


#   Program handling

def clear_program():
    global active_prog, active_prog_file, auto_schedule, series_next_due, sweep_schedule
    active_prog = None
    active_prog_file = ""
    auto_schedule = {}
    series_next_due = None
    sweep_schedule = {}
    update_yaml_flags("pantilt_status", {"active_program": "", "program_type": "",
                                         "program_next_index": 0, "program_paused": 0,
                                         "program_active_sweep": -1})


def pause_program(reason):
    update_yaml_flag("pantilt_status", "program_paused", 1)
    if reason:
        set_fault(reason)


def load_active_program(config):
    #   Cache the parsed program; (re)load and re-validate when the active file
    #   changes (daemon start, run_program, or after a limits/home change)
    global active_prog, active_prog_file, auto_schedule, series_next_due, sweep_schedule

    program_file = config.get("pantilt_status", {}).get("active_program", "")
    if not program_file:
        active_prog = None
        active_prog_file = ""
        return None

    if program_file == active_prog_file:
        return active_prog

    active_prog = None
    active_prog_file = program_file
    auto_schedule = {}
    series_next_due = None
    sweep_schedule = {}

    try:
        with open(program_file, "r") as f:
            prog = pantilt_program.load_program(f.read())
    except (OSError, pantilt_program.ProgramError) as e:
        pause_program(f"Cannot load program '{program_file}': {e}")
        return None

    #   Recovering an already-running program (daemon restart): re-apply its
    #   recorded home/orientation the same way a fresh start would
    pantilt_cfg = config.get("pantilt", {})
    overrides = pantilt_program.home_orientation_overrides(prog, pantilt_cfg)
    if overrides:
        update_yaml_flags("pantilt", overrides)
        pantilt_cfg = dict(pantilt_cfg, **overrides)

    reports = pantilt_program.validate_points(prog, pantilt_cfg)
    conflicts = pantilt_program.validate_schedule(prog, pantilt_cfg)
    if any(r["status"] == "error" for r in reports) or conflicts:
        pause_program(f"Program '{program_file}' is invalid for the current limits/home position")
        return None

    if prog["type"] == "automated":
        now_s = int(time.time())
        ref_epoch = prog["initial_startdate_epoch"]
        auto_schedule = {i: pantilt_program.next_occurrence(p, now_s, ref_epoch) for i, p in enumerate(prog["points"])}
    elif prog.get("sweeps") is not None:
        now_s = int(time.time())
        sweep_schedule = {i: pantilt_program.next_occurrence(
            {"repeated_minutes": s["repeat_minutes"], "offset_sec": 0}, now_s, s["initial_startdate_epoch"])
            for i, s in enumerate(prog["sweeps"])}

    active_prog = prog
    if prog.get("sweeps") is not None:
        n_points = sum(len(s["points"]) for s in prog["sweeps"])
        print(f"▶️ Program loaded: {program_file} ({prog['type']}, {len(prog['sweeps'])} sweeps, {n_points} points)")
    else:
        print(f"▶️ Program loaded: {program_file} ({prog['type']}, {len(prog['points'])} points)")
    return active_prog


def estimate_remaining_seconds(prog, index, pan_rel, tilt_rel, pantilt_cfg):
    #   Same distance/settle/measure model as pantilt_program.summarize(), but
    #   starting from the current live position instead of home, over only
    #   the points not yet done
    speed = max(float(pantilt_cfg.get("speed_deg_per_s_estimate", 4.0)), 0.1)
    settle = float(pantilt_cfg.get("settle_seconds", 2))
    measure_estimate = pantilt_program.MEASURE_SECONDS_ESTIMATE

    duration = 0.0
    pan, tilt = pan_rel, tilt_rel
    for point in prog["points"][index:]:
        duration += max(abs(point["pan_deg"] - pan), abs(point["tilt_deg"] - tilt)) / speed
        duration += settle + measure_estimate
        pan, tilt = point["pan_deg"], point["tilt_deg"]
    return duration


def run_single_series_tick(config):
    #   Execute ONE point per main-loop pass, so pause/stop/disable are handled
    #   between points by the normal loop
    global series_next_due
    pantilt_cfg = config.get("pantilt", {})
    prog = active_prog
    index = int(config.get("pantilt_status", {}).get("program_next_index", 0))
    repeat_minutes = prog.get("repeat_minutes")

    #   Between sweeps (repeat_minutes only): idle until the next grid slot,
    #   same offset_sec=0 grid logic an automated point uses
    if repeat_minutes and index == 0 and series_next_due is not None:
        now_s = time.time()
        live["next_measurement_utc"] = datetime.fromtimestamp(
            series_next_due, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        live["next_measurement_in_s"] = max(0, int(series_next_due - now_s))
        if now_s < series_next_due:
            return
        series_next_due = None

    if index >= len(prog["points"]):
        if repeat_minutes:
            ref_epoch = prog["initial_startdate_epoch"]
            series_next_due = pantilt_program.next_occurrence(
                {"repeated_minutes": repeat_minutes, "offset_sec": 0}, int(time.time()), ref_epoch)
            update_yaml_flag("pantilt_status", "program_next_index", 0)
            print(f"✅ Sweep finished, next sweep at "
                  f"{datetime.fromtimestamp(series_next_due, tz=timezone.utc)} UTC")
        else:
            print("✅ Single series finished")
            clear_program()
        return

    live["program_progress"] = f"{index + 1}/{len(prog['points'])}"
    live["steps_done"] = index
    live["steps_total"] = len(prog["points"])
    live["upcoming_points"] = [{"pan_rel": p["pan_deg"], "tilt_rel": p["tilt_deg"]}
                               for p in prog["points"][index:index + 3]]
    live["estimated_remaining_s"] = int(estimate_remaining_seconds(
        prog, index, live["pan_rel"], live["tilt_rel"], pantilt_cfg))

    result = run_point(prog["points"][index], pantilt_cfg, prog["defaults"]["try"])

    if result == "unreachable":
        print(f"⚠️ Point {index + 1}: instrument unreachable, skipping to the next point")

    if result in ("done", "unreachable"):
        update_yaml_flag("pantilt_status", "program_next_index", index + 1)
        if index + 1 >= len(prog["points"]) and not repeat_minutes:
            print("✅ Single series finished")
            clear_program()
    elif result == "fault":
        pause_program(live["fault"] or f"Point {index + 1} failed")


def run_multi_sweep_tick(config):
    #   Independently-scheduled named sweeps (prog["sweeps"]); only one runs
    #   at a time (one physical positioner). A sweep in progress always runs
    #   to completion (one point per tick, same as run_single_series_tick)
    #   before another sweep can start; idle ticks pick whichever due sweep
    #   was declared earliest in the file when more than one is due at once,
    #   and never skip a late sweep -- it just runs once it gets a turn.
    global sweep_schedule
    pantilt_cfg = config.get("pantilt", {})
    prog = active_prog
    sweeps = prog["sweeps"]
    status_cfg = config.get("pantilt_status", {})
    active_sweep = int(status_cfg.get("program_active_sweep", -1))
    index = int(status_cfg.get("program_next_index", 0))
    now_s = time.time()

    if active_sweep >= 0:
        sweep = sweeps[active_sweep]

        if index >= len(sweep["points"]):
            ref_epoch = sweep["initial_startdate_epoch"]
            sweep_schedule[active_sweep] = pantilt_program.next_occurrence(
                {"repeated_minutes": sweep["repeat_minutes"], "offset_sec": 0}, int(now_s), ref_epoch)
            update_yaml_flags("pantilt_status", {"program_active_sweep": -1, "program_next_index": 0})
            print(f"✅ Sweep '{sweep['name']}' finished, next at "
                  f"{datetime.fromtimestamp(sweep_schedule[active_sweep], tz=timezone.utc)} UTC")
            return

        live["program_progress"] = f"{sweep['name']}: {index + 1}/{len(sweep['points'])}"
        live["steps_done"] = index
        live["steps_total"] = len(sweep["points"])
        live["upcoming_points"] = [{"pan_rel": p["pan_deg"], "tilt_rel": p["tilt_deg"]}
                                   for p in sweep["points"][index:index + 3]]
        live["estimated_remaining_s"] = int(estimate_remaining_seconds(
            {"points": sweep["points"]}, index, live["pan_rel"], live["tilt_rel"], pantilt_cfg))

        result = run_point(sweep["points"][index], pantilt_cfg, prog["defaults"]["try"])

        if result == "unreachable":
            print(f"⚠️ Sweep '{sweep['name']}' point {index + 1}: instrument unreachable, skipping to the next point")

        if result in ("done", "unreachable"):
            update_yaml_flag("pantilt_status", "program_next_index", index + 1)
        elif result == "fault":
            pause_program(live["fault"] or f"Sweep '{sweep['name']}' point {index + 1} failed")
        return

    #   Idle between sweeps: is any sweep due? Earliest due time wins; ties
    #   (declared same instant) broken by lowest declared index (file order)
    if not sweep_schedule:
        return

    due_index = min(sweep_schedule, key=lambda i: (sweep_schedule[i], i))
    due = sweep_schedule[due_index]
    live["program_progress"] = f"waiting — next: '{sweeps[due_index]['name']}'"
    live["next_measurement_utc"] = datetime.fromtimestamp(due, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    live["next_measurement_in_s"] = max(0, int(due - now_s))
    live["steps_done"] = 0
    live["steps_total"] = 0

    if now_s < due:
        return

    update_yaml_flags("pantilt_status", {"program_active_sweep": due_index, "program_next_index": 0})
    print(f"▶️ Starting sweep '{sweeps[due_index]['name']}'")


def run_automated_series_tick(config):
    #   Fire the earliest due point; between occurrences the loop just idles
    pantilt_cfg = config.get("pantilt", {})
    prog = active_prog
    now_s = time.time()

    if not auto_schedule:
        return

    index = min(auto_schedule, key=auto_schedule.get)
    due = auto_schedule[index]
    live["next_measurement_utc"] = datetime.fromtimestamp(due, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    live["next_measurement_in_s"] = max(0, int(due - now_s))
    live["next_pan_rel"] = prog["points"][index]["pan_deg"]
    live["next_tilt_rel"] = prog["points"][index]["tilt_deg"]

    if now_s < due:
        return

    point = prog["points"][index]
    period = point["repeated_minutes"] * 60
    lateness = now_s - due

    ref_epoch = prog["initial_startdate_epoch"]

    if lateness > min(float(pantilt_cfg.get("min_gap_seconds", 60)), period / 2):
        #   Too late (previous point overran, pause, reboot): skip this occurrence
        print(f"⚠️ Skipping point {index + 1}: {int(lateness)} s late")
        auto_schedule[index] = pantilt_program.next_occurrence(point, int(now_s), ref_epoch)
        return

    live["program_progress"] = f"point {index + 1}/{len(prog['points'])}"
    result = run_point(point, pantilt_cfg, prog["defaults"]["try"])
    auto_schedule[index] = pantilt_program.next_occurrence(point, int(time.time()), ref_epoch)

    if result == "unreachable":
        print(f"⚠️ Point {index + 1}: instrument unreachable, will retry at the next scheduled occurrence")
    elif result == "fault":
        pause_program(live["fault"] or f"Point {index + 1} failed")


#   Dashboard one-shot commands (writer sets the flag, the daemon executes and clears it)

def process_commands(config):
    pantilt_cfg = config.get("pantilt", {})
    status_cfg = config.get("pantilt_status", {})
    program_active = status_cfg.get("active_program", "") != ""
    program_paused = status_cfg.get("program_paused", 0) == 1

    #   Settings changed (limits, speeds, ...)
    if pantilt_cfg.get("update", 0) == 1:
        update_yaml_flag("pantilt", "update", 0)
        apply_positioner_settings(pantilt_cfg)

        if pantilt_cfg.get("keepout_profile", "") != _keepout_profile_loaded:
            load_keepout(pantilt_cfg)

        if not target_allowed(live["pan_abs"], live["tilt_abs"], pantilt_cfg):
            set_fault("Current position is outside the new absolute limits — move back inside them")
        if program_active:
            #   Force re-validation of the active program against the new limits
            global active_prog_file
            active_prog_file = ""

    if pantilt_cfg.get("clear_fault", 0) == 1:
        update_yaml_flag("pantilt", "clear_fault", 0)
        driver.clear_faults()
        set_fault("")

    if pantilt_cfg.get("stop_program", 0) == 1:
        update_yaml_flag("pantilt", "stop_program", 0)
        if program_active:
            driver.stop()
            clear_program()
            print("🛑 Program stopped")
        return

    if pantilt_cfg.get("pause_program", 0) == 1:
        update_yaml_flag("pantilt", "pause_program", 0)
        if program_active and not program_paused:
            pause_program("")
            print("⏸️ Program paused")

    if pantilt_cfg.get("resume_program", 0) == 1:
        update_yaml_flag("pantilt", "resume_program", 0)
        if program_active and program_paused:
            driver.clear_faults()
            set_fault("")
            update_yaml_flag("pantilt_status", "program_paused", 0)
            print("▶️ Program resumed")

    if pantilt_cfg.get("run_program", ""):
        program_file = pantilt_cfg["run_program"]
        update_yaml_flag("pantilt", "run_program", "")
        start_program(program_file, config)
        return

    #   Manual (calibration) commands: not while a program is actively running
    if program_active and not program_paused:
        return

    if pantilt_cfg.get("set_home", 0) == 1:
        update_yaml_flag("pantilt", "set_home", 0)
        if program_active:
            set_fault("Stop the active program before changing the home position")
        elif live["moving"]:
            set_fault("Cannot set the home position while moving")
        else:
            update_yaml_flags("pantilt", {"home_pan_abs": live["pan_abs"], "home_tilt_abs": live["tilt_abs"]})
            print(f"🏠 Home position set to pan {live['pan_abs']}° / tilt {live['tilt_abs']}° (absolute)")
            poll_status(retrieve_yaml_file().get("pantilt", {}))
            persist_position(pantilt_cfg)

    if pantilt_cfg.get("step_request", 0) == 1:
        update_yaml_flag("pantilt", "step_request", 0)
        move_abs(live["pan_abs"] + float(pantilt_cfg.get("step_pan", 0.0)),
                 live["tilt_abs"] + float(pantilt_cfg.get("step_tilt", 0.0)), pantilt_cfg)

    if pantilt_cfg.get("move_request", 0) == 1:
        update_yaml_flag("pantilt", "move_request", 0)
        move_abs(float(pantilt_cfg.get("home_pan_abs", 0.0)) + float(pantilt_cfg.get("move_pan_rel", 0.0)),
                 float(pantilt_cfg.get("home_tilt_abs", 0.0)) + float(pantilt_cfg.get("move_tilt_rel", 0.0)),
                 pantilt_cfg)

    if pantilt_cfg.get("measure_request", 0) == 1:
        update_yaml_flag("pantilt", "measure_request", 0)
        if live["moving"]:
            set_fault("Cannot measure while moving")
        else:
            #   Same blocking primitive a program uses per point (measure_timeout_seconds);
            #   set_fault/"aborted" handling for a bad outcome already lives inside measure().
            live["measuring"] = 1
            result = measure(pantilt_cfg)
            live["measuring"] = 0
            if result == "done":
                print(f"✅ Manual measurement done at pan {live['pan_abs']}° / tilt {live['tilt_abs']}°")


def start_program(program_file, config):
    #   Validate before activating; refuse invalid programs with a clear fault
    pantilt_cfg = config.get("pantilt", {})
    try:
        with open(program_file, "r") as f:
            prog = pantilt_program.load_program(f.read())
    except (OSError, pantilt_program.ProgramError) as e:
        set_fault(f"Cannot start program '{program_file}': {e}")
        return None

    #   Apply the program's recorded home position / axis orientation (if any)
    #   BEFORE validating against limits: the program was designed for that
    #   setup, and the dashboard has already warned the user this would happen
    overrides = pantilt_program.home_orientation_overrides(prog, pantilt_cfg)
    if overrides:
        update_yaml_flags("pantilt", overrides)
        pantilt_cfg = dict(pantilt_cfg, **overrides)

    reports = pantilt_program.validate_points(prog, pantilt_cfg)
    conflicts = pantilt_program.validate_schedule(prog, pantilt_cfg)
    if any(r["status"] == "error" for r in reports) or conflicts:
        set_fault(f"Program '{program_file}' is invalid for the current limits/home position")
        return None

    set_fault("")
    update_yaml_flags("pantilt_status", {"active_program": program_file, "program_type": prog["type"],
                                         "program_next_index": 0, "program_paused": 0,
                                         "program_active_sweep": -1})
    return prog["type"]


def update_live_program(config):
    status_cfg = config.get("pantilt_status", {})
    live["program_name"] = os.path.basename(status_cfg.get("active_program", ""))
    live["program_type"] = status_cfg.get("program_type", "")
    live["fault"] = status_cfg.get("fault", live["fault"])

    if not status_cfg.get("active_program", ""):
        live["program_state"] = ""
        live["program_progress"] = ""
        live["next_measurement_utc"] = ""
        live["next_measurement_in_s"] = 0
        live["next_pan_rel"] = 0.0
        live["next_tilt_rel"] = 0.0
        live["steps_done"] = 0
        live["steps_total"] = 0
        live["upcoming_points"] = []
        live["estimated_remaining_s"] = 0
    elif status_cfg.get("program_paused", 0) == 1:
        live["program_state"] = "paused"
    else:
        live["program_state"] = "running"


def main_loop():
    global retry_started, next_retry

    #   Migrate an already-deployed pan-tilt config file
    ensure_yaml_section("pantilt", PANTILT_DEFAULTS)
    ensure_yaml_section("pantilt_status", PANTILT_STATUS_DEFAULTS)
    update_yaml_flag("pantilt_status", "connected", 0)

    load_keepout(retrieve_yaml_file().get("pantilt", {}))

    retry_started = time.time()
    next_retry = time.time()

    while True:

        time.sleep(0.1)

        config = retrieve_yaml_file()
        pantilt_cfg = config.get("pantilt", {})
        enabled = pantilt_cfg.get("enabled", 0) == 1
        live["pantilt_enabled"] = 1 if enabled else 0

        #   Module switched off: program state is kept, resumes when re-enabled
        if not enabled:
            if driver is not None:
                print("🔌 Pan-tilt module disabled")
                disconnect(stop_motion=True)
            retry_started = time.time()
            next_retry = time.time()
            continue

        #   Not connected: retry ladder (10 s for 5 minutes, then every 30 minutes)
        if driver is None:
            live["retry_in_s"] = max(0, int(next_retry - time.time()))
            if time.time() < next_retry:
                continue
            live["connecting"] = 1
            connected_now = try_connect(config)
            live["connecting"] = 0
            if not connected_now:
                interval = RETRY_FAST_S if time.time() - retry_started < RETRY_FAST_WINDOW_S else RETRY_SLOW_S
                next_retry = time.time() + interval
                print(f"🔎 Pan-tilt positioner not found, retrying in {interval} s")
            continue

        #   Connected
        try:
            poll_status(pantilt_cfg)
            update_live_program(config)
            process_commands(config)

            #   Program execution (pause/stop/disable are handled between points)
            config = retrieve_yaml_file()
            status_cfg = config.get("pantilt_status", {})
            if status_cfg.get("active_program", "") and status_cfg.get("program_paused", 0) == 0:
                prog = load_active_program(config)
                if prog is not None:
                    if prog["type"] == "single":
                        if prog.get("sweeps") is not None:
                            run_multi_sweep_tick(config)
                        else:
                            run_single_series_tick(config)
                    else:
                        run_automated_series_tick(config)

        except (ConnectionLost, qpt90.QptError, OSError) as e:
            print(f"❌ Pan-tilt connection lost: {e}")
            disconnect()
            retry_started = time.time()
            next_retry = time.time()


if __name__ == "__main__":

    threading.Thread(target=start_server, daemon=True).start()

    main_loop()
