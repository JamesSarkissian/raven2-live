#!/usr/bin/env python3
"""Find potholes and cracks with the trained model.

As a module (the viewer uses this):
    from detect import Detector
    det = Detector("models/road_damage/best.keras")
    for cls, score, x0, y0, x1, y1 in det.detect_jpeg(jpeg_bytes): ...

From the command line (TensorFlow environment):
    ~/PotholeRecognitionModel/tf_env/bin/python detect.py IMAGE_OR_FOLDER... [--out results/]
    ~/PotholeRecognitionModel/tf_env/bin/python detect.py recordings/<name> --out results/ --drone
--drone adds the haze/glare correction and square sections used for the
drone's own camera (detect_worker.py does the same for the live viewer).
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

    def detect_drone(self, bgr):
        """For the drone's own camera: contrast correction for its haze and
        glare, then the model on square sections of the frame (the left,
        middle and right of a 16:9 image) so nothing is squashed. Overlapping
        repeats from neighbouring sections are merged."""
        rgb = cv2.cvtColor(enhance(bgr), cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        s = min(h, w)
        xs = sorted({0, (w - s) // 2, w - s})
        ys = sorted({0, (h - s) // 2, h - s})
        found = []
        for x in xs:
            for y in ys:
                found += [(c, sc, x0 + x, y0 + y, x1 + x, y1 + y)
                          for c, sc, x0, y0, x1, y1 in self.detect_rgb(rgb[y:y + s, x:x + s])]
        found.sort(key=lambda d: -d[1])
        keep = []
        for d in found:
            if all(k[0] != d[0] or dm.iou(k[2:], d[2:]) < 0.3 for k in keep):
                keep.append(d)
        return keep


def enhance(bgr):
    """Even out the drone camera's bright haze/glare and boost local contrast.
    The brightness map is worked out on a small copy, so this takes a few ms."""
    h, w = bgr.shape[:2]
    small = cv2.resize(bgr, (max(1, w // 8), max(1, h // 8)), interpolation=cv2.INTER_AREA).astype(np.float32)
    glow = cv2.resize(cv2.GaussianBlur(small, (0, 0), small.shape[1] / 12), (w, h), interpolation=cv2.INTER_LINEAR)
    flat = np.clip(bgr / np.maximum(glow, 1) * 128, 0, 255).astype(np.uint8)
    lab = cv2.cvtColor(flat, cv2.COLOR_BGR2LAB)
    lab[..., 0] = cv2.createCLAHE(2.5, (8, 8)).apply(lab[..., 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


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
    ap.add_argument("--drone", action="store_true",
                    help="drone-camera mode: haze/glare correction and square sections (as the live viewer uses)")
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
        dets = det.detect_drone(bgr) if args.drone else det.detect_rgb(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
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
