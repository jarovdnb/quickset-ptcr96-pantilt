#!/usr/bin/env python3
#   Standalone tool (QPT-90 / PTCR-96) for mapping the pan/tilt keep-out zone
#   -- the coupled antenna/mounting-bar envelope enforced in pantilt.py's
#   move_abs() via lib/keepout.py (see that module's docstring for the model).
#   Does not touch pantilt_config.yaml.
#
#   STOP the daemon first -- a serial port can only be opened by one process:
#       sudo systemctl stop pantilt.service
#       python pt_keepout_record.py [/dev/ttyUSB0] [--name INSTRUMENT_NAME]
#       sudo systemctl start pantilt.service
#
#   Workflow: pick a pan angle, jog tilt in SMALL steps (checking antenna
#   clearance BY EYE after each move) until the antennas are JUST about to
#   touch the bar, record it with "r", then repeat from the other tilt
#   direction at the same pan -- don't assume the safe range is symmetric.
#   Repeat at a handful of pan angles (e.g. every 10-15 deg across the full
#   -180 to 180 sweep) to map out the taper -- lib/keepout.py is indexed by
#   pan, expecting one contiguous safe tilt band per pan for this mount's
#   geometry. Made a mistake? "u" undoes the last recorded sample.
#
#   Samples are saved to config/keepout/<INSTRUMENT_NAME>.json -- one file
#   per instrument/mount, since different antenna geometry means a different
#   safe envelope. Pick it from the dashboard's Advanced Settings "Keep-out
#   profile" dropdown afterward to make it the active restriction.

import argparse
import json
import os
import sys
import time

from lib import qpt90
from lib.configuration import retrieve_yaml_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEEPOUT_DIR = os.path.join(BASE_DIR, "config", "keepout")
MOVE_TIMEOUT_S = 15


def parse_args():
    parser = argparse.ArgumentParser(description="Map the pan/tilt antenna/bar keep-out zone.")
    parser.add_argument("port", nargs="?", default="", help="serial port (default: auto-scan / config)")
    parser.add_argument("--name", default="default", help="instrument/mount name (default: 'default')")
    return parser.parse_args()


def wait_settled(dev):
    deadline = time.time() + MOVE_TIMEOUT_S
    status = dev.get_status()
    while time.time() < deadline and status.moving:
        time.sleep(0.2)
        status = dev.get_status()
    return status


def samples_path(name):
    return os.path.join(KEEPOUT_DIR, f"{name}.json")


def load_samples(name):
    path = samples_path(name)
    if os.path.exists(path):
        with open(path, "r") as f:
            samples = json.load(f)
        print(f"Loaded {len(samples)} existing sample(s) from {path}")
        return samples
    return []


def save_samples(name, samples):
    os.makedirs(KEEPOUT_DIR, exist_ok=True)
    with open(samples_path(name), "w") as f:
        json.dump(samples, f, indent=2)


def print_samples(samples):
    if not samples:
        print("No samples recorded yet.")
        return
    for s in sorted(samples, key=lambda s: s["pan_deg"]):
        print(f"  pan={s['pan_deg']:+.2f} deg  ->  boundary tilt={s['tilt_deg']:+.2f} deg")


def print_help():
    print("Commands:")
    print("  j pan <deg>    jog pan by <deg> (e.g. 'j pan 1.0' / 'j pan -0.5')")
    print("  j tilt <deg>   jog tilt by <deg>")
    print("  r              record current position as a boundary sample")
    print("  u              undo the last recorded sample")
    print("  l              list recorded samples (sorted by tilt)")
    print("  h              show this help")
    print("  q              quit")


def main():
    args = parse_args()
    cfg = retrieve_yaml_file().get("pantilt", {})
    port_hint = args.port or cfg.get("port", "")
    baud = int(cfg.get("baud", 9600))

    print(f"Connecting (port={port_hint or 'auto-scan'}, baud={baud})...")
    port, dev = qpt90.find_qpt90(port_hint=port_hint, baud=baud)
    if dev is None:
        print("FAILED: no pan-tilt responded. Candidate ports tried:", qpt90.list_candidate_ports())
        sys.exit(1)
    print(f"Connected on {port}")

    name = args.name
    samples = load_samples(name)
    print(f"Recording into profile '{name}' ({samples_path(name)})")
    print_help()

    try:
        while True:
            status = dev.get_status()
            print(f"\n[{name}] pan={status.pan_deg:+.2f} deg  tilt={status.tilt_deg:+.2f} deg  "
                  f"faults: {', '.join(status.faults) if status.faults else 'none'}")
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()

            if cmd == "q":
                break
            elif cmd == "h":
                print_help()
            elif cmd == "l":
                print_samples(samples)
            elif cmd == "r":
                status = dev.get_status()
                sample = {"tilt_deg": round(status.tilt_deg, 2), "pan_deg": round(status.pan_deg, 2)}
                samples.append(sample)
                save_samples(name, samples)
                print(f"Recorded: {sample}  ({len(samples)} total)")
            elif cmd == "u":
                if samples:
                    removed = samples.pop()
                    save_samples(name, samples)
                    print(f"Removed: {removed}  ({len(samples)} remain)")
                else:
                    print("Nothing to remove.")
            elif cmd == "j" and len(parts) == 3 and parts[1].lower() in ("pan", "tilt"):
                try:
                    delta = float(parts[2])
                except ValueError:
                    print("Usage: j <pan|tilt> <deg>")
                    continue
                if parts[1].lower() == "pan":
                    dev.move_delta(pan_deg=delta, tilt_deg=0.0)
                else:
                    dev.move_delta(pan_deg=0.0, tilt_deg=delta)
                status = wait_settled(dev)
                print(f"after: pan={status.pan_deg:+.2f} deg  tilt={status.tilt_deg:+.2f} deg")
            else:
                print("Unknown command.")
                print_help()
    finally:
        dev.ser.close()

    print(f"\nFinal samples for '{name}':")
    print_samples(samples)


if __name__ == "__main__":
    main()
