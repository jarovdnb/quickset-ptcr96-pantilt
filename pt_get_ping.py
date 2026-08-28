#!/usr/bin/env python3
#   Standalone connectivity test: connect to the QPT-90 (PTCR-96) pan-tilt and
#   print its status once. Does not touch pantilt_config.yaml or the daemon.
#
#   Run from the pantilt/ folder:
#       python pt_get_ping.py            (auto-scan for the positioner)
#       python pt_get_ping.py /dev/ttyUSB0   (force a specific port)

import sys

from lib import qpt90
from lib.configuration import retrieve_yaml_file


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
    status = dev.get_status()
    print(f"pan={status.pan_deg:.1f} deg  tilt={status.tilt_deg:.1f} deg")
    print(f"moving={status.moving}  hard_limit={status.hard_limit}  soft_limit={status.soft_limit}  continuous={status.continuous}")
    print(f"faults: {', '.join(status.faults) if status.faults else 'none'}")

    dev.ser.close()


if __name__ == "__main__":
    main()
