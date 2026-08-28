#   MOOG QuickSet PTCR-96 embedded controller protocol driver (MN00162 Rev C).
#   Covers the Q90 (and other PTCR-96-based platforms: QPT50/90/200/500,
#   QMP/QMP-R) -- NOT the same wire format as the old lib/qpt.py, which
#   targeted an older/different protocol revision (removed once this app
#   moved to a PTCR-96/QPT-90 unit). Key differences from that old driver:
#     - Frames carry an Identity byte (STX, Identity, Cmd, Data, LRC, ETX);
#       the LRC covers Identity..last data byte, not just Cmd..data.
#     - Pan/tilt coordinates are 24-bit (3-byte) signed little-endian,
#       scaled x100 (0.01 deg), not 16-bit x10.
#     - The 31H status/jog command carries jog fields for two camera ports
#       (7 data bytes), not one.
#   Camera/lens/preset-table/tour/OSD/angle-correction/soft-limit commands
#   (60H-75H, 32H, 37H, 40H-56H, 80H-85H) are intentionally not implemented --
#   this app has no camera/lens hardware and does limits/inversion in its own
#   config instead of the PTCR's. Everything else in MN00162 Sec 2.9 IS
#   implemented: heater (97H), comm timeout (96H), max speed (9CH), firmware
#   revision (9AH), ramp parameters (92H), encoder align (9DH), homing cycle
#   (9EH), identity (9FH).
#
#   Frame:   STX  Identity  Cmd  [data...]  LRC  ETX     (host -> PTCR)
#            ACK  Identity  Cmd  [data...]  LRC  ETX     (PTCR -> host, NAK on error)
#   LRC:     XOR of Identity through last data byte
#   Escape:  any data/LRC byte equal to a control char is sent as ESC, byte|0x80
#   Ints:    24-bit signed little-endian, angle x100 (0.01 degree)
#
#   Transport: open_serial()/find_qpt90() for a direct RS-232/422 link
#   (pyserial). open_tcp()/find_qpt90_tcp() for this unit's "IP option" --
#   a Lantronix serial-to-Ethernet bridge wired to the same RS-232 port.
#   Selected in pantilt.py's try_connect() via the config's `transport`
#   field ("serial", the default, or "ip"). Same frames either way; only
#   the byte transport underneath Qpt90 differs.

import time

#   Control characters
STX = 0x02
ETX = 0x03
ACK = 0x06
NAK = 0x15
ESC = 0x1B
RESERVED = (STX, ETX, ACK, NAK, ESC)

#   Identity: 0 = dedicated RS-232/RS-422 or broadcast (MN00162 Sec 1.4)
BROADCAST_IDENTITY = 0x00

#   Commands (MN00162 Sec 2.1) actually implemented here
CMD_STATUS_JOG = 0x31   # get status / jog (doubles as keep-alive)
CMD_MOVE_ABS   = 0x33   # move to entered (absolute) coordinates
CMD_MOVE_DELTA = 0x34   # move to delta (relative) coordinates
CMD_MOVE_ZERO  = 0x35   # move to absolute 0/0
CMD_MOVE_HOME  = 0x36   # move to home (preset 31)
CMD_RAMP_PARAMS  = 0x92   # get/set pan & tilt ramp (accel/decel/start-stop) parameters
CMD_COMM_TIMEOUT = 0x96   # get/set communication timeout
CMD_HEATER       = 0x97   # get/set heater configuration
CMD_FIRMWARE_REV = 0x9A   # get firmware revision
CMD_MAX_SPEED    = 0x9C   # get/set/store maximum speed
CMD_ENCODER_ALIGN = 0x9D  # initial encoder align (pan center)
CMD_HOMING_CYCLE  = 0x9E  # perform homing cycle (hunt for index pulse, both axes)
CMD_IDENTITY      = 0x9F  # get/set RS-485 daisy-chain identity address

#   31H command bitset bits (Sec 2.2)
BIT_RES  = 0x01   # reset latched faults
BIT_STOP = 0x02   # stop all motion
BIT_OSL  = 0x04   # override soft limits
BIT_TSLO = 0x08   # halve tilt jog speed range
BIT_PSLO = 0x10   # halve pan jog speed range
BIT_TDIR = 0x40   # tilt jog direction: 1 = up, 0 = down
BIT_PDIR = 0x80   # pan jog direction: 1 = CW, 0 = CCW

#   "Move To Entered Coordinates" sentinel: leave this axis where it is
NO_MOVE_DEG = 999.99

#   97H Config byte (Sec 2.9.7): bit 7 = Query (1 = read the current mode
#   without changing it; only valid in a request). Bits 6-0 = mode -- in a
#   Query=0 request this is the desired mode, in any response (which always
#   has bit 7 = 0) this is the mode actually in effect.
HEATER_QUERY_BIT = 0x80
HEATER_OFF   = 0   # "No Heat" -- heater disabled, reduces overall current draw
HEATER_SHARE = 1   # heater cycles off while the axis motors are moving (caps peak current)
HEATER_FULL  = 2   # heater runs concurrently with motor operation

#   96H Timeout byte (Sec 2.9.8): bit 7 = Query, bits 6-0 = seconds (0-120).
#   0 disables ("defeats") the comm-timeout fault entirely.
COMM_TIMEOUT_QUERY_BIT = 0x80
COMM_TIMEOUT_DISABLED = 0

#   92H bitset (Sec 2.9.4): Query lives on the P Start/Stop byte only: bit 7
#   there = Query (read current values without changing them). The T
#   Start/Stop byte's bit 7 is unused/reserved. Reserve bytes are always 0.
RAMP_QUERY_BIT = 0x80

#   9CH Pan/Tilt bitset (Sec 2.9.10): bit 7 = Query, bit 6 = STOR (write to
#   non-volatile memory, loaded again at power-up, instead of just the
#   current session's volatile value).
MAX_SPEED_QUERY_BIT = 0x80
MAX_SPEED_STOR_BIT  = 0x40

#   9FH New Identity byte (Sec 2.9.14): bit 7 = Query, bits 6-0 = address.
#   0 = dedicated RS-232/RS-422 or broadcast (this driver's default and the
#   only mode this app uses -- identity only matters on a shared RS-485
#   daisy chain).
IDENTITY_QUERY_BIT = 0x80


class QptError(Exception):
    pass

class QptTimeout(QptError):
    pass

class QptFrameError(QptError):
    pass

class QptNak(QptError):
    pass


#   Pure frame helpers

def escape_bytes(data):
    out = bytearray()
    for b in data:
        if b in RESERVED:
            out.append(ESC)
            out.append(b | 0x80)
        else:
            out.append(b)
    return bytes(out)


def unescape_bytes(data):
    out = bytearray()
    esc = False
    for b in data:
        if esc:
            out.append(b & 0x7F)
            esc = False
        elif b == ESC:
            esc = True
        else:
            out.append(b)
    if esc:
        raise QptFrameError("Dangling escape byte")
    return bytes(out)


def lrc(payload):
    result = 0
    for b in payload:
        result ^= b
    return result


def encode_frame(lead, identity, cmd, data=b""):
    #   lead = STX for host frames, ACK/NAK for positioner replies
    payload = bytes([identity, cmd]) + bytes(data)
    return bytes([lead]) + escape_bytes(payload + bytes([lrc(payload)])) + bytes([ETX])


def decode_frame(raw):
    #   Returns (lead, identity, cmd, data); raises QptFrameError on a malformed frame
    if len(raw) < 4 or raw[0] not in (STX, ACK, NAK) or raw[-1] != ETX:
        raise QptFrameError(f"Malformed frame: {raw.hex()}")

    payload = unescape_bytes(raw[1:-1])
    if len(payload) < 3:
        raise QptFrameError(f"Frame too short: {raw.hex()}")

    #   XOR of identity..data..LRC must be 0
    if lrc(payload) != 0:
        raise QptFrameError(f"LRC mismatch: {raw.hex()}")

    return raw[0], payload[0], payload[1], payload[2:-1]


def deg_to_i24le(deg, scale=100):
    raw = int(round(deg * scale))
    return raw.to_bytes(3, "little", signed=True)


def i24le_to_deg(data, scale=100):
    return int.from_bytes(data[:3], "little", signed=True) / scale


class Qpt90Status:
    #   Decoded response to 31H/33H/34H/35H/36H: coordinates + pan/tilt/general
    #   status bytes (MN00162 Sec 2.2). Zoom/focus/camera fields that may follow
    #   are not parsed -- this driver doesn't drive the camera/lens ports.

    def __init__(self, pan_deg, tilt_deg, pan_status, tilt_status, gen_status):
        self.pan_deg = pan_deg
        self.tilt_deg = tilt_deg
        self.pan_status = pan_status
        self.tilt_status = tilt_status
        self.gen_status = gen_status

    @property
    def continuous(self):
        #   CON: platform is continuous rotation; pan soft/hard limits are ignored
        return bool(self.gen_status & 0x80)

    @property
    def executing(self):
        return bool(self.gen_status & 0x40)

    @property
    def destination(self):
        #   DES: returned coordinates are the destination, not the current position
        return bool(self.gen_status & 0x20)

    @property
    def moving(self):
        #   EXEC/DES or any of the four axis move bits
        return bool(self.gen_status & 0x6F)

    @property
    def hard_limit(self):
        return bool(self.pan_status & 0x30) or bool(self.tilt_status & 0x30)

    @property
    def soft_limit(self):
        return bool(self.pan_status & 0xC0) or bool(self.tilt_status & 0xC0)

    @property
    def faults(self):
        #   Latched faults (cleared with the RES bit): TO/DE per axis
        names = [(0x08, "timeout (TO)"), (0x04, "direction error (DE)")]
        result = []
        for axis, status in (("pan", self.pan_status), ("tilt", self.tilt_status)):
            for bit, name in names:
                if status & bit:
                    result.append(f"{axis} {name}")
        return result


class Qpt90:
    #   Driver around any pyserial-like object (write/read/timeout).
    #   Enforces the "no faster than 10 packets/sec" limit from MN00162 Sec 1.

    def __init__(self, ser, identity=BROADCAST_IDENTITY, min_tx_interval=0.1):
        self.ser = ser
        self.identity = identity
        self.min_tx_interval = min_tx_interval
        self.last_tx = 0.0

    def _read_frame(self, timeout):
        #   Collect bytes until a raw ETX (a real ETX is always a frame end thanks to escaping)
        deadline = time.time() + timeout
        raw = bytearray()
        while time.time() < deadline:
            byte = self.ser.read(1)
            if not byte:
                continue
            #   Skip garbage before the frame start
            if not raw and byte[0] not in (ACK, NAK):
                continue
            raw += byte
            if byte[0] == ETX:
                return bytes(raw)
        raise QptTimeout("No response from positioner")

    def transact(self, cmd, data=b"", timeout=1.0):
        #   Rate limit
        wait = self.last_tx + self.min_tx_interval - time.time()
        if wait > 0:
            time.sleep(wait)

        if hasattr(self.ser, "reset_input_buffer"):
            self.ser.reset_input_buffer()

        self.ser.write(encode_frame(STX, self.identity, cmd, data))
        self.last_tx = time.time()

        lead, identity, rcmd, rdata = decode_frame(self._read_frame(timeout))
        if lead == NAK:
            raise QptNak(f"NAK for command {cmd:02X}")
        if rcmd != cmd:
            raise QptFrameError(f"Reply command {rcmd:02X} does not match {cmd:02X}")
        return rdata

    def _parse_status(self, data):
        #   Common prefix of every 31H/33H/34H/35H/36H response: pan(3) + tilt(3)
        #   + pan_status(1) + tilt_status(1) + gen_status(1). Zoom/focus/camera
        #   bytes that may follow are ignored.
        if len(data) < 9:
            raise QptFrameError(f"Status response too short: {data.hex()}")

        return Qpt90Status(
            pan_deg=i24le_to_deg(data[0:3]),
            tilt_deg=i24le_to_deg(data[3:6]),
            pan_status=data[6],
            tilt_status=data[7],
            gen_status=data[8],
        )

    def get_status(self, stop=False, res=False, osl=False, pan_jog=0, tilt_jog=0):
        #   31H: keep-alive + status. pan_jog/tilt_jog are signed speeds
        #   (-255..255); sign selects direction (PDIR/TDIR), magnitude is the
        #   jog speed byte. 0 means "don't jog that axis".
        bitset = (BIT_STOP if stop else 0) | (BIT_RES if res else 0) | (BIT_OSL if osl else 0)
        if pan_jog > 0:
            bitset |= BIT_PDIR
        if tilt_jog > 0:
            bitset |= BIT_TDIR
        pan_speed = min(255, abs(pan_jog))
        tilt_speed = min(255, abs(tilt_jog))

        #   Trailing 4 bytes are zoom/focus jog for camera 1 and 2 (unused here)
        data = self.transact(CMD_STATUS_JOG, bytes([bitset, pan_speed, tilt_speed, 0, 0, 0, 0]))
        return self._parse_status(data)

    def move_to(self, pan_deg=None, tilt_deg=None):
        #   33H: absolute move; None leaves that axis where it is
        pan_val = NO_MOVE_DEG if pan_deg is None else pan_deg
        tilt_val = NO_MOVE_DEG if tilt_deg is None else tilt_deg
        data = self.transact(CMD_MOVE_ABS, deg_to_i24le(pan_val) + deg_to_i24le(tilt_val))
        return self._parse_status(data)

    def move_delta(self, pan_deg=0.0, tilt_deg=0.0):
        #   34H: relative move; 0 leaves that axis where it is
        data = self.transact(CMD_MOVE_DELTA, deg_to_i24le(pan_deg) + deg_to_i24le(tilt_deg))
        return self._parse_status(data)

    def move_to_zero(self):
        #   35H: move to absolute (factory center) 0/0
        return self._parse_status(self.transact(CMD_MOVE_ZERO))

    def move_to_home(self):
        #   36H: move to the "Home" preset (preset 31)
        return self._parse_status(self.transact(CMD_MOVE_HOME))

    def stop(self):
        return self.get_status(stop=True)

    def clear_faults(self):
        return self.get_status(res=True)

    def get_heater_config(self):
        #   97H with the Query bit set: reads the current mode without changing it
        data = self.transact(CMD_HEATER, bytes([HEATER_QUERY_BIT]))
        if not data:
            raise QptFrameError("Heater query response was empty")
        return data[0] & 0x7F

    def set_heater_config(self, config):
        #   97H with the Query bit clear: 0=No Heat, 1=Share (off while
        #   moving), 2=Full Heat (concurrent with motion). The unit echoes
        #   back the mode it actually accepted; a mismatch means the request
        #   was not honored (e.g. no heater fitted on this unit).
        if config not in (HEATER_OFF, HEATER_SHARE, HEATER_FULL):
            raise ValueError(f"heater config must be {HEATER_OFF}, {HEATER_SHARE} or {HEATER_FULL}")
        data = self.transact(CMD_HEATER, bytes([config & 0x7F]))
        if not data:
            raise QptFrameError("Heater set response was empty")
        return data[0] & 0x7F

    def get_comm_timeout(self):
        #   96H with the Query bit set: seconds before the positioner treats
        #   a lost link as a fault and stops (0 = disabled/"defeat")
        data = self.transact(CMD_COMM_TIMEOUT, bytes([COMM_TIMEOUT_QUERY_BIT]))
        if not data:
            raise QptFrameError("Comm-timeout query response was empty")
        return data[0] & 0x7F

    def set_comm_timeout(self, seconds):
        #   96H with the Query bit clear: 0-120 seconds; 0 disables the
        #   comm-loss fault entirely (all other faults remain active)
        if not 0 <= seconds <= 120:
            raise ValueError("comm timeout must be 0-120 seconds")
        data = self.transact(CMD_COMM_TIMEOUT, bytes([seconds & 0x7F]))
        if not data:
            raise QptFrameError("Comm-timeout set response was empty")
        return data[0] & 0x7F

    def get_max_speed(self, stored=False):
        #   9CH with the Query bit set: current volatile (session) values,
        #   or -- with STOR also set -- the stored non-volatile defaults
        bitset = MAX_SPEED_QUERY_BIT | (MAX_SPEED_STOR_BIT if stored else 0)
        data = self.transact(CMD_MAX_SPEED, bytes([bitset, 0, 0]))
        if len(data) < 3:
            raise QptFrameError(f"Max-speed query response too short: {data.hex()}")
        return data[1], data[2]

    def set_max_speed(self, pan_max, tilt_max, stored=False):
        #   9CH with the Query bit clear: writes the volatile (session-only)
        #   values, or -- with STOR set -- to non-volatile memory (loaded
        #   again at power-up)
        if not (1 <= pan_max <= 255 and 1 <= tilt_max <= 255):
            raise ValueError("max speed must be 1-255 per axis")
        bitset = MAX_SPEED_STOR_BIT if stored else 0
        data = self.transact(CMD_MAX_SPEED, bytes([bitset, pan_max & 0xFF, tilt_max & 0xFF]))
        if len(data) < 3:
            raise QptFrameError(f"Max-speed set response too short: {data.hex()}")
        return data[1], data[2]

    def get_firmware_revision(self):
        #   9AH: no request data; response is major, minor, day, month,
        #   year (0-99, representing 2000-2099)
        data = self.transact(CMD_FIRMWARE_REV)
        if len(data) < 5:
            raise QptFrameError(f"Firmware revision response too short: {data.hex()}")
        return {"major": data[0], "minor": data[1], "day": data[2], "month": data[3], "year": 2000 + data[4]}

    def get_ramp_params(self):
        #   92H with the Query bit set (on the P Start/Stop byte): current
        #   accel/decel/ramp tuning for both axes. See MN00162 Sec 2.9.4 for
        #   what these values mean -- they're platform/load-dependent.
        data = self.transact(CMD_RAMP_PARAMS, bytes([RAMP_QUERY_BIT, 0, 1, 0, 0, 0, 1, 0]))
        if len(data) < 8:
            raise QptFrameError(f"Ramp-params query response too short: {data.hex()}")
        return {
            "pan_start_stop": data[0], "pan_acc_dec": data[1], "pan_ramp": data[2],
            "tilt_start_stop": data[4], "tilt_acc_dec": data[5], "tilt_ramp": data[6],
        }

    def set_ramp_params(self, pan_start_stop, pan_acc_dec, pan_ramp,
                         tilt_start_stop, tilt_acc_dec, tilt_ramp):
        #   92H with the Query bit clear: writes accel/decel/ramp tuning for
        #   both axes to non-volatile memory. Derive these by testing (MN00162
        #   Sec 2.9.4) -- there's no safe platform-independent default.
        data = self.transact(CMD_RAMP_PARAMS, bytes([
            pan_start_stop & 0x7F, pan_acc_dec & 0xFF, pan_ramp & 0xFF, 0,
            tilt_start_stop & 0x7F, tilt_acc_dec & 0xFF, tilt_ramp & 0xFF, 0,
        ]))
        if len(data) < 8:
            raise QptFrameError(f"Ramp-params set response too short: {data.hex()}")
        return {
            "pan_start_stop": data[0], "pan_acc_dec": data[1], "pan_ramp": data[2],
            "tilt_start_stop": data[4], "tilt_acc_dec": data[5], "tilt_ramp": data[6],
        }

    def initial_encoder_align(self, timeout=15.0):
        #   9DH: BLOCKS on the unit while it hunts for the pan index pulse --
        #   the positioner stops responding to anything else until this
        #   completes, so this needs a longer-than-default timeout.
        data = self.transact(CMD_ENCODER_ALIGN, timeout=timeout)
        if len(data) < 6:
            raise QptFrameError(f"Encoder-align response too short: {data.hex()}")
        return {"pan_index_deg": i24le_to_deg(data[0:3]), "tilt_index_deg": i24le_to_deg(data[3:6])}

    def perform_homing_cycle(self, timeout=30.0):
        #   9EH: BLOCKS on the unit while it hunts for the index pulse on
        #   both axes and returns to its original (corrected) position --
        #   can take several seconds, needs a longer-than-default timeout.
        data = self.transact(CMD_HOMING_CYCLE, timeout=timeout)
        if len(data) < 7:
            raise QptFrameError(f"Homing-cycle response too short: {data.hex()}")
        bitset = data[0]
        return {
            "pan_index_found": bool(bitset & 0x01),
            "tilt_index_found": bool(bitset & 0x02),
            "pan_offset_deg": i24le_to_deg(data[1:4]),
            "tilt_offset_deg": i24le_to_deg(data[4:7]),
        }

    def get_identity(self):
        #   9FH with the Query bit set, sent at this driver's current
        #   identity (0 = broadcast/dedicated, this app's default, will make
        #   any attached unit answer)
        data = self.transact(CMD_IDENTITY, bytes([IDENTITY_QUERY_BIT]))
        if not data:
            raise QptFrameError("Identity query response was empty")
        return data[0] & 0x7F

    def set_identity(self, new_identity):
        #   9FH with the Query bit clear: changes the unit's RS-485
        #   daisy-chain address. Must be sent addressed to the unit's CURRENT
        #   identity (self.identity) -- irrelevant on a dedicated RS-232/
        #   RS-422 link (identity 0), which is everything this app uses.
        if not 0 <= new_identity <= 99:
            raise ValueError("identity must be 0-99")
        data = self.transact(CMD_IDENTITY, bytes([new_identity & 0x7F]))
        if not data:
            raise QptFrameError("Identity set response was empty")
        confirmed = data[0] & 0x7F
        self.identity = confirmed
        return confirmed


def open_serial(port, baud):
    #   Imported here, not at module level, so importing this module (e.g. for
    #   protocol-only unit tests) never requires pyserial to be installed
    import serial
    return serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                         parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                         timeout=0.05)


def connect(ser, identity=BROADCAST_IDENTITY, attempts=20, should_abort=None):
    #   The unit autobauds at power-up: it needs ~125-150 bytes before it starts
    #   replying, so keep sending status polls until one is answered.
    #   should_abort (optional, no-arg callable) is polled between attempts so a
    #   caller can bail out of a slow scan early -- e.g. pantilt.py aborting when
    #   the module gets disabled mid-connect. Kept generic (no config knowledge
    #   here) to stay a pure protocol driver.
    qpt = Qpt90(ser, identity=identity)
    for _ in range(attempts):
        if should_abort is not None and should_abort():
            return None
        try:
            qpt.get_status()
            return qpt
        except (QptTimeout, QptFrameError, QptNak):
            continue
    return None


def list_candidate_ports():
    #   Cross-platform port discovery (Windows COM ports, Linux /dev/tty*,
    #   macOS /dev/cu.*) via pyserial's own device listing.
    from serial.tools import list_ports
    return sorted(p.device for p in list_ports.comports())


def find_qpt90(port_hint="", baud=9600, identity=BROADCAST_IDENTITY, should_abort=None):
    #   Try the configured port first, otherwise probe every detected serial port
    candidates = [port_hint] if port_hint else list_candidate_ports()

    for port in candidates:
        if should_abort is not None and should_abort():
            return None, None
        ser = None
        try:
            ser = open_serial(port, baud)
            qpt = connect(ser, identity=identity, should_abort=should_abort)
            if qpt is not None:
                return port, qpt
            ser.close()
        except Exception as e:
            print(f"⚠️ Could not probe {port}: {e}")
            if ser is not None:
                ser.close()

    return None, None


#   IP transport: this unit's "IP option" is a Lantronix serial-to-Ethernet
#   bridge (XPort) wired directly to the PTCR-96's own RS-232 port -- it is
#   NOT a native network stack on the controller. Raw bytes sent to its
#   tunnel port are piped straight to/from that serial line unmodified, so
#   the exact same STX...ETX frames above go out unchanged; only the
#   transport underneath the Qpt90 class differs. connect() above already
#   works with any duck-typed write()/read() object, so nothing about the
#   protocol layer needs to change for this.

class _TcpTransport:
    #   Duck-types the small subset of a pyserial object Qpt90 actually uses:
    #   write(bytes), read(n) -> up to n bytes (b"" on timeout, never blocks
    #   forever), reset_input_buffer(), close().

    def __init__(self, host, port, read_timeout=0.2, connect_timeout=5):
        import socket
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.read_timeout = read_timeout
        self.sock.settimeout(read_timeout)

    def write(self, data):
        self.sock.sendall(data)

    def read(self, n=1):
        import socket
        try:
            return self.sock.recv(n) or b""
        except socket.timeout:
            return b""

    def reset_input_buffer(self):
        #   Unlike a fresh serial port, this is one persistent TCP connection
        #   making many sequential requests -- a reply that arrives late (or
        #   any unread leftover byte) stays sitting in the socket's receive
        #   buffer until the next read() picks it up, which transact() would
        #   otherwise mistake for the NEXT request's reply. Drain whatever is
        #   already buffered before every write, matching what pyserial's
        #   reset_input_buffer() does on a real port. (A real, observed bug
        #   this driver had when reset_input_buffer() was a no-op: a stale
        #   9CH max-speed reply got picked up as the following 97H heater
        #   reply, tripping the "reply command doesn't match" check.)
        import socket
        self.sock.settimeout(0)
        try:
            while self.sock.recv(4096):
                pass
        except (BlockingIOError, socket.timeout, OSError):
            pass
        finally:
            self.sock.settimeout(self.read_timeout)

    def close(self):
        self.sock.close()


def open_tcp(host, port, read_timeout=0.2, connect_timeout=5):
    #   Imported lazily inside _TcpTransport, not at module level, matching
    #   open_serial()'s convention (importing this module for protocol-only
    #   unit tests never requires network access)
    return _TcpTransport(host, port, read_timeout=read_timeout, connect_timeout=connect_timeout)


def find_qpt90_tcp(host, port, identity=BROADCAST_IDENTITY, attempts=20, should_abort=None):
    #   No candidate scanning needed (a single fixed host:port, unlike
    #   find_qpt90()'s serial-port probing) -- just open the tunnel and reuse
    #   the same retry-until-responsive loop connect() already does for a
    #   fresh serial link.
    if not host:
        return None, None
    try:
        ser = open_tcp(host, port)
    except OSError as e:
        print(f"⚠️ Could not reach {host}:{port}: {e}")
        return None, None

    qpt = connect(ser, identity=identity, attempts=attempts, should_abort=should_abort)
    if qpt is None:
        ser.close()
        return None, None
    return f"{host}:{port}", qpt
