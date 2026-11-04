#!/usr/bin/env python3
"""Pretend to be the Raven 2 on this Mac, for testing without the drone.

Serves camera frames on 127.0.0.1:7060 and replays captured flight data on
127.0.0.1:50000 to whoever sends the telemetry request. Then run:
    ./live_viewer.py 127.0.0.1 --port 8766

Usage:
    ./fake_drone.py FRAMES_DIR [UDP_TEXT] [--start SECONDS] [--speed X]
FRAMES_DIR  folder of frame_*.jpg (decode_stream.py output)
UDP_TEXT    `tcpdump -tt -x` text of UDP port 50000 from a capture
"""
import argparse
import glob
import os
import socket
import struct
import threading
import time

import telemetry as tm
from udp_parse import packets

MAGIC = b"lewei_cmd\x00"
DRONE_SRC = "192.168.0.1:50000"


def serve_video(frames_dir, stall=0.0, every=0.0):
    """stall/every: every `every` seconds, freeze for `stall` seconds in the
    middle of a frame, like a weak Wi-Fi link."""
    frames = [open(f, "rb").read() for f in sorted(glob.glob(os.path.join(frames_dir, "frame_*.jpg")))]
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 7060))
    srv.listen(1)
    while True:
        conn, _ = srv.accept()
        i = 0
        next_stall = time.time() + every
        try:
            while True:
                f = frames[i % len(frames)]
                i += 1
                body = struct.pack("<LLQL", 0, len(f), i, 0) + b"\0" * 12 + f
                msg = MAGIC + struct.pack("<9I", 257, 0, 0, len(body), 0, 0, 0, 0, 0) + body
                if stall and time.time() >= next_stall:
                    half = len(msg) // 2
                    conn.sendall(msg[:half])
                    print("stalling %.0f s mid-frame" % stall, flush=True)
                    time.sleep(stall)
                    conn.sendall(msg[half:])
                    next_stall = time.time() + every
                else:
                    conn.sendall(msg)
                time.sleep(1 / 15)
        except OSError:
            conn.close()


def serve_telemetry(udp_text, start, speed):
    data = [(t, p) for t, s, d, p in packets(udp_text) if s == DRONE_SRC]
    first = [i for i, (t, _) in enumerate(data) if t >= data[0][0] + start][0]
    data = data[first:]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", tm.TELEMETRY_PORT))
    peer = [None]

    def listen():
        while True:
            msg, addr = sock.recvfrom(2048)
            if not tm.is_request(msg):
                print("unexpected message from viewer:", msg.hex())
            peer[0] = addr

    threading.Thread(target=listen, daemon=True).start()
    while peer[0] is None:
        time.sleep(0.1)
    print("viewer asked for flight data; replaying %d packets at %gx" % (len(data), speed))
    t0, w0 = data[0][0], time.time()
    for t, p in data:
        delay = (t - t0) / speed - (time.time() - w0)
        if delay > 0:
            time.sleep(delay)
        sock.sendto(p, peer[0])
    print("flight data replay finished")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("frames")
    ap.add_argument("udp_text", nargs="?")
    ap.add_argument("--start", type=float, default=0, help="seconds into the capture")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--stall", type=float, default=0, help="freeze video this long...")
    ap.add_argument("--every", type=float, default=10, help="...every this many seconds")
    args = ap.parse_args()
    threading.Thread(target=serve_video, args=(args.frames, args.stall, args.every), daemon=True).start()
    if args.udp_text:
        serve_telemetry(args.udp_text, args.start, args.speed)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
