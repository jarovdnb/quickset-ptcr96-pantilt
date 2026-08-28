# Quickset PTCR-96 Pan-Tilt Controller

Controls a Quickset/MOOG pan-tilt positioner from a Raspberry Pi: a daemon that talks the
PTCR-96 "Embedded Controller Protocol" over serial or IP, a small Flask dashboard for
jogging/calibrating/scheduling, and a YAML-driven measurement-program scheduler. It runs the
positioner end-to-end on its own — the only thing it can't know for you is what "take a
measurement" means for your instrument, which is the single function you fill in (see
"Customizing" below).

Built and deployed against a **Quickset 7-7600B MWS**, but the driver targets the whole
PTCR-96-based family (see "Hardware" below) and should work unmodified on any QPT50/90/200/500 or
QMP/QMP-R unit. Fork it, point `lib/measurement.py` at your own instrument, and it's ready to run.

## Hardware

- **Positioner**: any Quickset/MOOG unit built on the PTCR-96 controller — QPT-90-family
  (this repo was built and tested against a **Quickset 7-7600B MWS**), QPT50/200/500, and
  QMP/QMP-R.
- **Embedded controller**: PTCR-96 ("Embedded Controller Protocol", MN00162 Rev C), built into
  the positioner base. `lib/qpt90.py` implements this protocol.
- **Connection**: a direct RS-232/422 link (USB-serial adapter into the controller's port, the
  `serial` transport) or, via a Lantronix "IP option" (an XPort serial-to-Ethernet bridge wired
  to that same RS-232 port), a TCP connection instead (the `ip` transport). See "Serial vs. IP
  transport" below.

## Architecture

- `pantilt.py` — the daemon (own process, own systemd unit, own Unix socket at
  `/tmp/pantilt_socket.sock`). Owns the serial port to the QPT-90, executes measurement programs,
  and polls its own config file every 100 ms for commands from `pantilt-dashboard/app_pantilt.py`.
- `pantilt-dashboard/app_pantilt.py` + `pantilt-dashboard/templates/index.html` — a small Flask
  dashboard (no login — see "Setup" below) for enabling the module, jogging/calibrating, and
  uploading/running measurement programs.
- `lib/qpt90.py` — pure PTCR-96 wire-protocol driver ("Embedded Controller Protocol", MN00162 Rev
  C, covers QPT50/90/200/500 and QMP/QMP-R). No config or instrument knowledge at all.
  Camera/lens/preset-table/tour commands and the heater/max-speed/comm-timeout setup commands are
  not implemented (their exact data layout wasn't available when the driver was written — see the
  module's header comment and "Cross-cutting concerns" below).
- `lib/pantilt_program.py` — YAML measurement-program parsing, validation and scheduling. Takes a
  plain `pantilt_cfg` dict; no config file I/O of its own.
- `lib/configuration.py` — config read/write helpers (`retrieve_yaml_file`, `update_yaml_flag`,
  `update_yaml_flags`, `ensure_yaml_section`), pointed at this app's own config file
  (`~/pantilt_config.yaml` by default, overridable via `PANTILT_CONFIG_PATH`).
- `lib/keepout.py` — optional coupled pan/tilt keep-out envelope, for mounts where the antenna/
  payload can strike part of the rig at some pan/tilt combinations but not others.
- `lib/measurement.py` — **the one file you edit.** See "Customizing" below.

### Serial vs. IP transport

`pantilt.py` can reach the PTCR-96 controller two ways, selected by the `transport` field in
`pantilt_config.yaml`:

|`transport`|Fields used|Driver entry points|Physical path|
|-|-|-|-|
|`serial` (default)|`port`, `baud`|`qpt90.find_qpt90()` / `open_serial()`|USB-RS232 adapter straight into the controller|
|`ip`|`host`, `tcp_port` (default `10001`)|`qpt90.find_qpt90_tcp()` / `open_tcp()`|Lantronix XPort serial-to-Ethernet bridge wired to the same RS-232 port|

Both paths speak the exact same STX…ETX protocol frames — `connect()` in `lib/qpt90.py` works with
either transport interchangeably, only the byte transport underneath changes. An already-deployed
`pantilt_config.yaml` that predates this feature and has no `transport`/`host`/`tcp_port` keys is
not a problem: `pantilt.py`'s `PANTILT_DEFAULTS` backfills them (`transport: serial`) automatically.

**Switching transport requires a power cycle** — see "Cross-cutting concerns" below.

## Customizing: hook up your own measurement

Everything in this app — moving the positioner, scheduling program points, keep-out enforcement,
the dashboard — works out of the box for any Quickset/PTCR-96-based pan-tilt unit. The one thing
it cannot know for you is what "take a measurement" means for *your* instrument. That single
customization point is `lib/measurement.py::take_measurement()`:

```python
def take_measurement(pan_deg: float, tilt_deg: float) -> str:
    ...
```

`pantilt.py` calls this once per scheduled program point, with the positioner already moved and
settled at `(pan_deg, tilt_deg)`. The call blocks the scheduler until it returns, so it's fine to
wait inside it for your own measurement to actually finish. Replace the placeholder body with
whatever triggers your instrument — call an API, run a script, write a flag file/GPIO pin, grab a
camera frame, whatever fits.

Return `"done"` on success. Return `"unreachable"` if your instrument couldn't be triggered this
time — the scheduler treats that as expected and recoverable (retries a few times, then moves on
to the next point) rather than raising a positioner fault. That's the entire integration surface;
no other file needs to change.

## Setup

1. Make sure user `pi` can open serial ports: `sudo usermod -a -G dialout pi` (logout/login
   afterwards).
2. Connect the PTCR-96 controller via the USB RS-232 adapter for the default `serial` transport;
   `pantilt.py` probes every serial port the OS reports automatically
   (`lib/qpt90.py:list_candidate_ports()`). To use the `ip` transport instead, wire the
   controller's RS-232 port through a Lantronix XPort bridge and set `host`/`tcp_port` in step 4
   — see "Serial vs. IP transport" above.
3. Optional: give the adapter a fixed name with a udev rule
   (`sudo nano /etc/udev/rules.d/52-qpt.rules`), filling in the vendor/product id from `lsusb`:

   ```text
   SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", SYMLINK+="qpt90", MODE:="0666"
   ```

   and set `/dev/qpt90` as the serial port in the dashboard's pan-tilt settings.
4. Copy this repo's `pantilt_config.yaml` template to `~/pantilt_config.yaml` and fill in the
   real serial port (or `transport`/`host`/`tcp_port` for the IP option) / limits / home position
   for your setup:

   ```bash
   cp ~/quickset-ptcr96-pantilt/pantilt_config.yaml ~/pantilt_config.yaml
   ```

5. Edit `lib/measurement.py` to hook up your own instrument — see "Customizing" above.
6. Install the required python packages:

   ```bash
   sudo python3 -m pip install pyyaml --break-system-packages
   sudo python3 -m pip install filelock --break-system-packages
   sudo python3 -m pip install flask --break-system-packages
   sudo python3 -m pip install pyserial --break-system-packages
   ```

7. Install and start the services:

   ```bash
   sudo cp ~/quickset-ptcr96-pantilt/pantilt.service /lib/systemd/system/
   sudo cp ~/quickset-ptcr96-pantilt/app_pantilt.service /lib/systemd/system/
   sudo systemctl enable pantilt.service app_pantilt.service
   sudo systemctl start pantilt.service app_pantilt.service
   ```

   With `pantilt.enabled: 0` in `~/pantilt_config.yaml` the daemon just sleeps.
8. The dashboard has **no login** — only run it on a network you trust (e.g. your home LAN, or
   behind your own VPN/reverse-proxy auth). Don't port-forward it or expose it directly to the
   internet.
9. Protocol/driver + program-engine + keep-out unit tests (no hardware needed):

   ```bash
   python3 tests/test_qpt90_frames.py
   python3 tests/test_pantilt_program.py
   python3 tests/test_keepout.py
   ```

10. Optional: map a pan/tilt keep-out zone for your mount geometry with `pt_keepout_record.py`
    (see "Cross-cutting concerns" below), then select the resulting profile from the dashboard's
    Advanced Settings.

## Internal structure

```text
lib/qpt90.py             STX…ETX framing, escaping/LRC, Qpt90 driver class, serial port discovery
                          (find_qpt90/open_serial) and IP transport (find_qpt90_tcp/open_tcp)
lib/keepout.py           coupled pan/tilt keep-out envelope (breakpoints, is_safe, safe_tilt_intersection_over_sweep)
lib/pantilt_program.py  load_program, validate_points/validate_schedule, scheduling math, preview()
lib/configuration.py    this app's own pantilt_config.yaml I/O (PyYAML + filelock)
lib/measurement.py      the one customization point -- take_measurement(pan_deg, tilt_deg)

pantilt.py:
  stream_data / start_server           Unix socket server (/tmp/pantilt_socket.sock)
  effective_limits / target_allowed    raw<->logical limit enforcement (inversion negates+swaps)
  set_fault / flip                     fault latch + axis-inversion helper
  load_keepout                         (re)loads config/keepout/<profile>.json into _keepout_breakpoints
  poll_status / persist_position       live telemetry + local config position cache
  apply_heater_config / apply_comm_timeout / apply_max_speed / apply_positioner_settings
  try_connect / disconnect             serial/IP connection lifecycle
  wait_move_done / _send_move / move_abs / settle
  measure                              calls lib/measurement.py's take_measurement() per point
  run_point                            per-point retry loop
  clear_program / pause_program / load_active_program / start_program
  run_single_series_tick / run_automated_series_tick
  process_commands                     reads pending flags from this app's own config each tick
  main_loop                            top-level scheduler

pt_keepout_record.py     standalone tool (bypasses the daemon) for mapping a keep-out profile
```

## Cross-cutting concerns worth knowing before touching this code

- **Axis inversion**: `pan_min_abs`/`pan_max_abs`/`tilt_min_abs`/`tilt_max_abs` are always the
  *raw hardware* travel range. Every consumer (`pantilt.py:effective_limits`,
  `pantilt_program.py:relative_limits`) must apply the same raw→logical conversion (`flip`/invert
  negates **and swaps** min/max) or limit enforcement and inversion will silently disagree.
- **Recorded setup backfill**: a program YAML's `home_pan_abs`/`home_tilt_abs`/
  `pan_orientation`/`tilt_orientation` are optional and independently specifiable. Undeclared ones
  are backfilled from the live config on first run (never overwritten later); declared ones are
  applied to the live pan-tilt config *before* validation when the program starts.
- **Heater / max-speed / comm-timeout are applied to hardware**: `lib/qpt90.py` implements the
  97H/9CH/96H commands, and `pantilt.py` sends them to the positioner on every connect and every
  settings update (`apply_heater_config`/`apply_max_speed`/`apply_comm_timeout`, chained from
  `apply_positioner_settings`). Heater mode is set then re-queried to confirm the unit actually
  accepted it (faults on mismatch — e.g. no heater fitted). Comm-timeout is fixed at 2s, not
  user-configurable (the positioner halts itself if the daemon dies or the link drops that long).
  Max-speed is session-only (re-applied on every reconnect from `pan_max_speed`/`tilt_max_speed`,
  not written to non-volatile memory). Still not implemented in `lib/qpt90.py`: camera/lens/
  preset-table/tour/OSD commands — this app has no camera hardware and doesn't use presets/tours.
- **Keep-out zone**: `lib/keepout.py` + `pantilt.py:move_abs` enforce a coupled pan/tilt envelope
  for mounts where an antenna/payload can strike part of the rig at some pan/tilt combinations but
  not others — a per-pan safe tilt range that a simple `tilt_min_abs`/`tilt_max_abs` box can't
  express. The active profile is `config/keepout/<pantilt_cfg.keepout_profile>.json`, selected
  from the dashboard's Advanced Settings; `""` means unrestricted. Map a profile for a new
  instrument/mount with `pt_keepout_record.py` (daemon must be stopped first, same
  one-port-one-process rule as the other `pt_*.py` scripts). **Gotcha**: breakpoints are measured
  and enforced against RAW hardware angles, not the logical `pan_abs`/`tilt_abs` the dashboard
  shows — `move_abs` converts via `flip` before checking; a mistake there silently defeats the
  safety check on inverted mounts. Coupled diagonal moves are sequenced as three single-axis legs
  (not a single diagonal `move_to`) through a pan value proven safe across the whole tilt sweep,
  since PTCR-90 has no coordinated-trajectory guarantee between two axes moving at once.
- **Switching transport needs a power cycle**: after changing `transport` (or `host`/`tcp_port`)
  in `pantilt_config.yaml`, physically power-cycle the PTCR-96 controller (unplug and reconnect
  its power) for the change to take effect. No software reboot/reset command for this was found in
  the PTCR-96 protocol (MN00162) — a physical power cycle is, as far as we've found, the only way
  to switch it between serial and IP mode.
