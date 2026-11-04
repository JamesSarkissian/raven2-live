#!/usr/bin/env python3
"""Pothole/crack detection for the live viewer, run as a helper process.

The viewer runs on the Mac's plain Python; the model needs the TensorFlow
environment, so the viewer starts this script with that Python when the
DETECT toggle is switched on, and stops it when switched off.

Protocol (stdin/stdout, one frame at a time):
  viewer -> worker: 4-byte little-endian length, then that many JPEG bytes
                    (length 0 = quit)
  worker -> viewer: one JSON line per frame:
                    {"w": .., "h": .., "ms": .., "dets": [[class, score, x0, y0, x1, y1], ...]}
                    box corners are fractions of the image (0..1)
At start it prints {"ready": true, "model": ...} or {"error": "..."}.
"""
import json
import os
import struct
import sys
import time

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def say(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "models", "road_damage", "best.keras")
    threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.25
    try:
        import cv2
        import numpy as np
        from detect import Detector
        if not os.path.exists(model):
            raise FileNotFoundError("no trained model at %s" % model)
        det = Detector(model, threshold)
        det.detect_drone(np.zeros((72, 128, 3), np.uint8))     # warm up so the first real frame is fast
    except Exception as e:      # report any start-up failure to the viewer
        say({"error": "%s: %s" % (type(e).__name__, e)})
        return 1
    say({"ready": True, "model": os.path.basename(model), "threshold": threshold})
    inp = sys.stdin.buffer
    while True:
        head = inp.read(4)
        if len(head) < 4:
            return 0
        n = struct.unpack("<I", head)[0]
        if n == 0:
            return 0
        jpeg = inp.read(n)
        t0 = time.time()
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            say({"w": 0, "h": 0, "ms": 0, "dets": []})
            continue
        h, w = bgr.shape[:2]
        dets = [[c, round(s, 3), round(x0 / w, 4), round(y0 / h, 4), round(x1 / w, 4), round(y1 / h, 4)]
                for c, s, x0, y0, x1, y1 in det.detect_drone(bgr)]
        say({"w": w, "h": h, "ms": round((time.time() - t0) * 1000), "dets": dets})


if __name__ == "__main__":
    sys.exit(main())
