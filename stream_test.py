#!/usr/bin/env python3
"""Video-only stream test for the Sky Rider Raven 2 (Lewei-style camera).

Run it while the Mac is joined to the drone's SkyRider_... Wi-Fi.
It talks ONLY to the camera's video port 7060 and sends ONLY three message
types, all printed in full before anything is sent:
  start stream (2), heartbeat (1, once per second), stop stream (3).
It never opens the movement port (9060) or the settings port (8060), and it
changes no setting on the drone.

Protocol layout from pylwdrone (Apache-2.0, github.com/meekworth/pylwdrone):
  header = b'lewei_cmd\\0' + 9 little-endian uint32
           [cmd, arg1, 0, body_size, stream_type, key1, key2, 0, 0]
  video body = flag(u32) size(u32) count(u64) gphoto(u32) pad -> frame at 32

Output goes to a stream_<time> folder next to this script:
  video.h264  best-effort H.264 for playback
  raw.bin     every message the camera sent, untouched, for offline decoding
  report.txt  summary
"""
import datetime
import os
import socket
import struct
import sys
import time

DRONE_IP = "192.168.0.1"
VIDEO_PORT = 7060          # the only port this script ever connects to
DURATION = 20              # seconds of video to record
HEARTBEAT_EVERY = 1.0

MAGIC = b"lewei_cmd\x00"
HDR_LEN = len(MAGIC) + 9 * 4   # 46 bytes
CMD_HEARTBEAT = 1
CMD_START = 2
CMD_STOP = 3
CMD_RET_STREAM = 257
ALLOWED_CMDS = {CMD_HEARTBEAT, CMD_START, CMD_STOP}


def build(cmd, arg1=0):
    assert cmd in ALLOWED_CMDS
    return MAGIC + struct.pack("<9I", cmd, arg1, 0, 0, 0, 0, 0, 0, 0)


START = build(CMD_START, 1)    # arg1=1 asks for high definition
HEARTBEAT = build(CMD_HEARTBEAT)
STOP = build(CMD_STOP)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("camera closed the connection")
        buf += chunk
    return bytes(buf)


def fix_type1(frame, size, count):
    # stream_type 1 flips one byte per frame; undo it (from pylwdrone)
    p1, p2 = count, size & 0xFFFFFFFF
    if p2 == 0:
        return
    if p2 & 1 == 0:
        v2 = (p2 + 1 + (p2 ^ p1)) ^ p2
    else:
        v2 = ((p2 ^ p1) + p2) ^ p2
    idx = (v2 - (v2 // p2) * p2) & 0xFFFFFFFF
    if idx < size:
        frame[idx] = ~frame[idx] & 0xFF


def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else DRONE_IP
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, datetime.datetime.now().strftime("stream_%Y%m%d_%H%M%S"))

    print("Bytes this script will send to %s:%d (and nowhere else):" % (ip, VIDEO_PORT))
    print("  start stream, once:   ", START.hex(" "))
    print("  heartbeat, every 1 s: ", HEARTBEAT.hex(" "))
    print("  stop stream, at end:  ", STOP.hex(" "))
    if input("Props off, remote bound, Mac on the drone Wi-Fi? Send these? [y/N] ").strip().lower() != "y":
        print("Cancelled. Nothing was sent.")
        return 1

    os.makedirs(out)
    report = open(os.path.join(out, "report.txt"), "w")

    def log(msg):
        print(msg)
        report.write(msg + "\n")
        report.flush()

    log("== Raven 2 stream test %s ==" % datetime.datetime.now())
    stats = {"frames": 0, "bytes": 0, "types": set(), "other": {}}
    sock = None
    t0 = time.time()
    try:
        sock = socket.create_connection((ip, VIDEO_PORT), timeout=10)
        with open(os.path.join(out, "raw.bin"), "wb") as raw, \
             open(os.path.join(out, "video.h264"), "wb") as h264:
            sock.sendall(START)
            log("sent start stream")
            t0 = last_hb = time.time()
            while time.time() - t0 < DURATION:
                hdr = recv_exact(sock, HDR_LEN)
                if hdr[:len(MAGIC)] != MAGIC:
                    log("unexpected header bytes: " + hdr.hex(" "))
                    raw.write(hdr + sock.recv(65536))
                    break
                ints = struct.unpack_from("<9I", hdr, len(MAGIC))
                cmd, body_size, stype = ints[0], ints[3], ints[4]
                body = recv_exact(sock, body_size)
                raw.write(hdr + body)
                if cmd == CMD_RET_STREAM and len(body) >= 32:
                    flag, size, count, _ = struct.unpack_from("<LLQL", body)
                    frame = bytearray(body[32:32 + size])
                    if stype == 1:
                        fix_type1(frame, size, count)
                    h264.write(frame)
                    stats["frames"] += 1
                    stats["bytes"] += len(frame)
                    stats["types"].add(stype)
                    if stats["frames"] == 1:
                        log("first frame: %d bytes, stream_type %d, starts %s"
                            % (size, stype, bytes(frame[:8]).hex(" ")))
                elif cmd != CMD_HEARTBEAT:
                    stats["other"][cmd] = stats["other"].get(cmd, 0) + 1
                if time.time() - last_hb >= HEARTBEAT_EVERY:
                    sock.sendall(HEARTBEAT)
                    last_hb = time.time()
            sock.sendall(STOP)
            log("sent stop stream")
    except (OSError, ConnectionError) as e:
        log("connection ended: %s" % e)
    finally:
        if sock:
            sock.close()

    secs = max(time.time() - t0, 0.001)
    log("frames: %d  (%.1f per second)" % (stats["frames"], stats["frames"] / secs))
    log("video bytes: %d" % stats["bytes"])
    log("stream types: %s" % sorted(stats["types"]))
    if 129 in stats["types"]:
        log("note: stream_type 129 scrambles 3 bytes per frame; Claude will fix it from raw.bin")
    if stats["other"]:
        log("other messages from camera: %s" % stats["other"])
    log("Results in: " + out)
    log("Reconnect to your normal Wi-Fi, then tell Claude the stream test is finished.")
    report.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
