#!/usr/bin/env python3
"""Turn a stream_test.py capture (raw.bin) into viewable JPEG frames.

Usage: ./decode_stream.py stream_YYYYMMDD_HHMMSS
Writes <dir>/frames/frame_0001.jpg ... and <dir>/viewer.html (open it to play).
"""
import os
import struct
import sys

from lw_unscramble import unscramble

MAGIC = b"lewei_cmd\x00"
HDR_LEN = len(MAGIC) + 9 * 4
CMD_RET_STREAM = 257


def frames(raw):
    pos = 0
    while pos + HDR_LEN <= len(raw):
        if raw[pos:pos + len(MAGIC)] != MAGIC:
            raise ValueError("lost sync at byte %d" % pos)
        ints = struct.unpack_from("<9I", raw, pos + len(MAGIC))
        body = raw[pos + HDR_LEN:pos + HDR_LEN + ints[3]]
        pos += HDR_LEN + ints[3]
        if ints[0] == CMD_RET_STREAM and len(body) >= 32:
            _, size, _, _ = struct.unpack_from("<LLQL", body)
            yield ints[4], ints[5], ints[6], bytearray(body[32:32 + size])


def main():
    d = sys.argv[1].rstrip("/")
    raw = open(os.path.join(d, "raw.bin"), "rb").read()
    out = os.path.join(d, "frames")
    os.makedirs(out, exist_ok=True)
    n = good = 0
    for stype, k1, k2, frame in frames(raw):
        if stype == 129:
            unscramble(frame, k1, k2)
        n += 1
        if frame[:2] == b"\xff\xd8":
            good += 1
        with open(os.path.join(out, "frame_%04d.jpg" % n), "wb") as f:
            f.write(frame)
    with open(os.path.join(d, "viewer.html"), "w") as f:
        f.write("""<!doctype html><meta charset=utf-8><title>Raven 2 capture</title>
<body style="margin:0;background:#111;color:#ddd;font:14px system-ui;text-align:center">
<img id=v style="max-width:100%%;max-height:92vh"><div id=c></div>
<script>let i=1,n=%d;const v=document.getElementById('v'),c=document.getElementById('c');
setInterval(()=>{v.src='frames/frame_'+String(i).padStart(4,'0')+'.jpg';c.textContent=i+' / '+n;i=i%%n+1},1000/15)</script>
""" % n)
    print("frames: %d, JPEG frames: %d" % (n, good))
    print("open %s" % os.path.join(d, "viewer.html"))


if __name__ == "__main__":
    main()
