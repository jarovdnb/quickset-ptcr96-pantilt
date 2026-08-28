#   Plain-assert tests for the QPT-90 / PTCR-96 protocol driver (no hardware needed).
#   Run with: python3 tests/test_qpt90_frames.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.qpt90 import (STX, ETX, ACK, BROADCAST_IDENTITY, QptFrameError, QptNak,
                       encode_frame, decode_frame, escape_bytes, unescape_bytes,
                       deg_to_i24le, i24le_to_deg, Qpt90Status)


def test_escaping():
    #   Every reserved byte must be escaped as ESC, byte|0x80 (manual examples: 02->1B 82, 1B->1B 9B)
    assert escape_bytes(bytes([0x02])) == bytes([0x1B, 0x82])
    assert escape_bytes(bytes([0x1B])) == bytes([0x1B, 0x9B])
    for b in (0x02, 0x03, 0x06, 0x15, 0x1B):
        assert unescape_bytes(escape_bytes(bytes([b]))) == bytes([b])
    #   Non-reserved bytes pass through untouched
    assert escape_bytes(bytes([0x00, 0x31, 0xFF])) == bytes([0x00, 0x31, 0xFF])


def test_frame_roundtrip():
    #   Round-trip with data containing all reserved bytes
    data = bytes([0x02, 0x03, 0x06, 0x15, 0x1B, 0x00, 0x7F, 0xFF])
    raw = encode_frame(STX, BROADCAST_IDENTITY, 0x31, data)
    lead, identity, cmd, decoded = decode_frame(raw)
    assert lead == STX and identity == BROADCAST_IDENTITY and cmd == 0x31 and decoded == data

    #   Manual worked example: keep-alive 31H, broadcast identity, all-zero
    #   jog/bitset data (7 bytes: bitset, pan_speed, tilt_speed, 2x zoom/focus
    #   jog) -> LRC is just cmd 0x31 (identity/data are all zero)
    raw = encode_frame(STX, BROADCAST_IDENTITY, 0x31, bytes(7))
    assert raw == bytes([0x02, 0x00, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x31, 0x03])

    #   Move-to pan 5.15, tilt 0.0: pan's low bytes 03 02 collide with ETX/STX
    #   and must both be escaped
    raw = encode_frame(STX, BROADCAST_IDENTITY, 0x33, deg_to_i24le(5.15) + deg_to_i24le(0.0))
    assert raw == bytes([0x02, 0x00, 0x33, 0x1B, 0x83, 0x1B, 0x82, 0x00, 0x00, 0x00, 0x00, 0x32, 0x03])


def test_lrc_corruption():
    raw = bytearray(encode_frame(ACK, BROADCAST_IDENTITY, 0x31, bytes([0x10, 0x20])))
    raw[3] ^= 0xFF   # corrupt a data byte
    try:
        decode_frame(bytes(raw))
        assert False, "corrupted frame must raise"
    except QptFrameError:
        pass


def test_int_encoding():
    #   Manual examples: 9000 (90.00 deg) -> 28 23 00, -2000 (-20.00 deg) -> 30 F8 FF
    assert deg_to_i24le(90.0) == bytes([0x28, 0x23, 0x00])
    assert deg_to_i24le(-20.0) == bytes([0x30, 0xF8, 0xFF])
    for deg in (180.0, -180.0, 90.0, -90.0, 0.0, -0.0, 217.5, 0.1):
        assert abs(i24le_to_deg(deg_to_i24le(deg)) - deg) < 1e-9


def test_status_decode():
    #   Idle at pan 10.0 / tilt -15.0, no faults
    status = Qpt90Status(10.0, -15.0, 0x00, 0x00, 0x00)
    assert not status.moving and status.faults == []

    #   Moving CW + EXEC
    status = Qpt90Status(0, 0, 0x00, 0x00, 0x48)
    assert status.moving and status.executing

    #   Latched faults: pan TO, tilt DE
    status = Qpt90Status(0, 0, 0x08, 0x04, 0x00)
    assert status.faults == ["pan timeout (TO)", "tilt direction error (DE)"]

    #   Limits
    assert Qpt90Status(0, 0, 0x20, 0x00, 0x00).hard_limit
    assert Qpt90Status(0, 0, 0x80, 0x00, 0x00).soft_limit

    #   Continuous-rotation platform flag
    assert Qpt90Status(0, 0, 0x00, 0x00, 0x80).continuous


if __name__ == "__main__":
    test_escaping()
    test_frame_roundtrip()
    test_lrc_corruption()
    test_int_encoding()
    test_status_decode()
    print("✅ All qpt90 frame tests passed")
