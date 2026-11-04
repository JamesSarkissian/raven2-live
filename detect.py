#!/usr/bin/env python3
"""Find potholes and cracks with the trained model.

As a module (the viewer uses this):
    from detect import Detector
    det = Detector("models/road_damage/best.keras")
    for cls, score, x0, y0, x1, y1 in det.detect_jpeg(jpeg_bytes): ...

From the command line (TensorFlow environment):
    ~/PotholeRecognitionModel/tf_env/bin/python detect.py IMAGE_OR_FOLDER... [--out results/]
    ~/PotholeRecognitionModel/tf_env/bin/python detect.py recordings/<name> --out results/
Writes copies of the images with boxes drawn (red = pothole, yellow = crack)
and prints what was found.
"""
import argparse
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import damage_model as dm  # noqa: E402

DEFAULT_MODEL = os.path.join(HERE, "models", "road_damage", "best.keras")
COLORS = {"pothole": (60, 60, 255), "crack": (0, 210, 255)}     # BGR


class Detector(object):
    def __init__(self, path=DEFAULT_MODEL, threshold=0.35):
        import tensorflow as tf
        self.model = tf.keras.models.load_model(path, compile=False)
        self.threshold = threshold
        self.size = int(self.model.input_shape[1])     # the size it was trained at

        @tf.function(reduce_retracing=True)
        def run(x):
            return self.model(x, training=False)
        self._run = run

    def detect_rgb(self, rgb):
        h, w = rgb.shape[:2]
        x = cv2.resize(rgb, (self.size, self.size), interpolation=cv2.INTER_AREA).astype(np.float32)[None]
        out = self._run(x).numpy()[0]
        return dm.decode(out, w, h, threshold=self.threshold)

    def detect_jpeg(self, jpeg):
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            return []
        return self.detect_rgb(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


def draw(bgr, dets):
    for cls, score, x0, y0, x1, y1 in dets:
        c = COLORS[cls]
        cv2.rectangle(bgr, (int(x0), int(y0)), (int(x1), int(y1)), c, 2)
        cv2.putText(bgr, "%s %.0f%%" % (cls, score * 100), (int(x0), max(12, int(y0) - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, c, 2)
    return bgr


def inputs(paths):
    """Yield (name, jpeg bytes) from image files, folders, or recordings."""
    for p in paths:
        if os.path.isfile(os.path.join(p, "frames.csv")):
            import csv
            with open(os.path.join(p, "frames.csv")) as f, open(os.path.join(p, "video.mjpg"), "rb") as v:
                for i, r in enumerate(csv.DictReader(f)):
                    v.seek(int(r["offset"]))
                    yield "%s_frame%05d.jpg" % (os.path.basename(os.path.normpath(p)), i), v.read(int(r["size"]))
        elif os.path.isdir(p):
            for n in sorted(os.listdir(p)):
                if n.lower().endswith((".jpg", ".jpeg", ".png")):
                    with open(os.path.join(p, n), "rb") as f:
                        yield n, f.read()
        else:
            with open(p, "rb") as f:
                yield os.path.basename(p), f.read()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--threshold", type=float, default=0.35)
    ap.add_argument("--out", help="folder for images with boxes drawn")
    args = ap.parse_args()
    det = Detector(args.model, args.threshold)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
    n = found = 0
    t0 = time.time()
    for name, jpeg in inputs(args.paths):
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        dets = det.detect_rgb(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        n += 1
        found += bool(dets)
        if dets:
            print("%s: %s" % (name, ", ".join("%s %.0f%%" % (d[0], d[1] * 100) for d in dets)))
        if args.out:
            cv2.imwrite(os.path.join(args.out, name if name.lower().endswith(".jpg") else name + ".jpg"), draw(bgr, dets))
    dt = time.time() - t0
    print("%d images, %d with damage, %.0f ms per image" % (n, found, 1000 * dt / max(n, 1)))


if __name__ == "__main__":
    main()
