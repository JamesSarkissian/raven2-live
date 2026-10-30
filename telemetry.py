"""Decode the Raven 2 flight-controller telemetry carried over UDP.

Worked out from a passive capture of the Sky Rider GPS app (2026-10-30) and
checked against the values the app displayed.

The phone sends one 18-byte request per second from UDP port 5010 to the
drone's port 50000; the drone answers ~10x per second with two frames.
Frames look like MSP v1 with a 'BT' preamble:
  'B' 'T' dir len cmd payload[len] xor(len,cmd,payload)   dir '>' drone, '<' phone
Each datagram holds one or more frames plus 4 trailing bytes the drone ignores.

  0x88 (7 bytes): roll, pitch int16 0.1 deg; yaw uint16 0.1 deg; state byte
  0x89 (19 bytes): distance int16 dm; horizontal speed int16 dm/s; altitude int16 dm;
                   vertical speed int16 dm/s; satellites; fix; lat, lon int32 1e-7 deg;
                   battery percent (sags under motor load)
Note: the Sky Rider app labels speeds "mph" but shows km/h, and its battery %
is the lowest value seen so far.
"""
import struct

PRE = b"BT"
TELEMETRY_PORT = 50000     # on the drone
LOCAL_PORT = 5010          # the drone answers to this port

# The exact request the Sky Rider app sends, copied byte for byte from the
# capture: "send 0x88" + "send 0x89", then 4 trailing bytes. It asks for
# data only; it carries no stick, throttle, arm or calibration value.
REQUEST = bytes.fromhex("42543c0188018842543c01890189cb4b31d9")

STATES = {0x20: "Locked", 0x22: "Unlocked", 0x58: "Landing"}


def frames(datagram):
    """Yield (direction, cmd, payload) for every frame with a valid checksum."""
    i = 0
    while i + 6 <= len(datagram) and datagram[i:i + 2] == PRE:
        d, n, cmd = datagram[i + 2], datagram[i + 3], datagram[i + 4]
        end = i + 5 + n
        if end >= len(datagram):
            break
        payload = datagram[i + 5:end]
        x = n ^ cmd
        for b in payload:
            x ^= b
        if x == datagram[end]:
            yield chr(d), cmd, payload
        i = end + 1


def decode(cmd, p):
    """Turn one drone frame into overlay fields (metric units)."""
    if cmd == 0x88 and len(p) == 7:
        roll, pitch, yaw = struct.unpack_from("<hhH", p)
        return {"roll_deg": roll / 10, "pitch_deg": pitch / 10, "yaw_deg": yaw / 10,
                "heading_deg": yaw / 10, "state": STATES.get(p[6], "0x%02x" % p[6])}
    if cmd == 0x89 and len(p) == 19:
        dist, hs, alt, vs, sats, fix, lat, lon, bat = struct.unpack("<hhhhBBiiB", p)
        return {"dist_m": dist / 10, "hspeed_ms": hs / 10, "alt_m": alt / 10,
                "vspeed_ms": vs / 10, "sats": sats, "gps_fix": bool(fix),
                "lat": lat / 1e7, "lon": lon / 1e7, "battery_raw": bat}
    return None


def is_request(datagram):
    """True only for the one request this project is allowed to send."""
    return datagram == REQUEST
