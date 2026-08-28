#!/usr/bin/env python3
#   Standalone motion test (QPT-90 / PTCR-96): move the pan axis +1 deg
#   (relative) and report the new position. Does not touch pantilt_config.yaml
#   or the daemon.
#
#   Run from the pantilt/ folder:
#       python pt_pan_plus_1deg.py            (auto-scan for the positioner)
#       python pt_pan_plus_1deg.py /dev/ttyUSB0   (force a specific port)

import sys
import time

from lib import qpt90
from lib.configuration import retrieve_yaml_file

DELTA_DEG = 1.0
MOVE_TIMEOUT_S = 15


def get_port_baud():
    cfg = retrieve_yaml_file().get("pantilt", {})
    port = sys.argv[1] if len(sys.argv) > 1 else cfg.get("port", "")
    baud = int(cfg.get("baud", 9600))
    return port, baud


def main():
    port_hint, baud = get_port_baud()
    print(f"Connecting (port={port_hint or 'auto-scan'}, baud={baud})...")

    port, dev = qpt90.find_qpt90(port_hint=port_hint, baud=baud)
    if dev is None:
        print("FAILED: no pan-tilt responded.")
        sys.exit(1)

    print(f"Connected on {port}")
    before = dev.get_status()
    print(f"pan before: {before.pan_deg:.1f} deg")

    print(f"Moving pan by {DELTA_DEG:+.1f} deg...")
    dev.move_delta(pan_deg=DELTA_DEG, tilt_deg=0.0)

    deadline = time.time() + MOVE_TIMEOUT_S
    status = dev.get_status()
    while time.time() < deadline and status.moving:
        time.sleep(0.2)
        status = dev.get_status()

    print(f"pan after:  {status.pan_deg:.1f} deg  (moving={status.moving})")
    print(f"faults: {', '.join(status.faults) if status.faults else 'none'}")

    dev.ser.close()


if __name__ == "__main__":
    main()
