#!/usr/bin/env python3
"""Turn drone recordings into images for labelling (more training data).

    python3 extract_frames.py recordings/<name> [more recordings...] [--every 1.0] [--out dataset/drone]

For each recording it saves:
  - one frame every --every seconds (default 1 s), and
  - every frame where the model found something, if DETECT was on while
    recording (at most one per --hit-gap seconds, default 0.5 s),
into --out/<recording>/, plus _annotations.csv in the same format as the
training data (filename,width,height,class,xmin,ymin,xmax,ymax). Its boxes are
the model's guesses only: open them in a labelling tool (e.g. Roboflow or
CVAT, "TensorFlow Object Detection CSV"), fix and add boxes, then train with
the corrected file. Frames with no guesses get a row with empty box columns
so they are still listed. frames.csv in each folder says when and where every
image was taken.

Plain Python, no TensorFlow needed. Only reads the recordings.
"""
import argparse
import bisect
import csv
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))


def jpeg_size(data):
    """(width, height) from the JPEG header, or (0, 0)."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return 0, 0


def read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def extract(folder, out_root, every, hit_gap):
    name = os.path.basename(os.path.normpath(folder))
    frames = [(float(r["t"]), int(r["offset"]), int(r["size"])) for r in read_rows(os.path.join(folder, "frames.csv"))]
    if not frames:
        print("%s: no frames, skipped" % name)
        return 0
    times = [f[0] for f in frames]
    tel = read_rows(os.path.join(folder, "telemetry.csv"))
    tel_t = [float(r["t"]) for r in tel]

    # model results, grouped by the frame they were run on
    hits = {}
    for r in read_rows(os.path.join(folder, "detections.csv")):
        hits.setdefault(r["frame_t"], []).append(r)

    def nearest(t):
        i = bisect.bisect_left(times, t)
        return min((j for j in (i - 1, i) if 0 <= j < len(times)), key=lambda j: abs(times[j] - t))

    chosen = {}     # frame index -> model boxes for it
    nxt = times[0]
    for i, t in enumerate(times):
        if t >= nxt:
            chosen[i] = []
            nxt = t + every
    last_hit = -1e9
    for ft in sorted(hits, key=float):
        j = nearest(float(ft))
        if j in chosen or float(ft) - last_hit >= hit_gap:
            chosen[j] = hits[ft]
            last_hit = float(ft)

    out = os.path.join(out_root, name)
    os.makedirs(out, exist_ok=True)
    n_boxes = 0
    with open(os.path.join(folder, "video.mjpg"), "rb") as v, \
            open(os.path.join(out, "_annotations.csv"), "w", newline="") as fa, \
            open(os.path.join(out, "frames.csv"), "w", newline="") as fi:
        ann = csv.writer(fa)
        ann.writerow(["filename", "width", "height", "class", "xmin", "ymin", "xmax", "ymax"])
        idx = csv.writer(fi)
        idx.writerow(["filename", "t", "lat", "lon", "alt_m", "heading", "model_boxes"])
        for i in sorted(chosen):
            t, off, size = frames[i]
            v.seek(off)
            jpeg = v.read(size)
            w, h = jpeg_size(jpeg)
            fn = "%s_%06.1f.jpg" % (name, t - times[0])
            with open(os.path.join(out, fn), "wb") as f:
                f.write(jpeg)
            boxes = chosen[i]
            for b in boxes:
                ann.writerow([fn, w, h, b["class"]] + [round(float(b[k]) * (w if k[0] == "x" else h))
                                                       for k in ("x0", "y0", "x1", "y1")])
            if not boxes:
                ann.writerow([fn, w, h, "", "", "", "", ""])
            n_boxes += len(boxes)
            k = bisect.bisect_right(tel_t, t) - 1
            r = tel[k] if k >= 0 else {}
            idx.writerow([fn, "%.3f" % t, r.get("lat", ""), r.get("lon", ""), r.get("alt_m", ""),
                          r.get("heading", ""), len(boxes)])
    print("%s: %d images (%d with model guesses, %d boxes) -> %s"
          % (name, len(chosen), sum(1 for b in chosen.values() if b), n_boxes, out))
    return len(chosen)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("recordings", nargs="+")
    ap.add_argument("--every", type=float, default=1.0, help="seconds between regular frames (default 1)")
    ap.add_argument("--hit-gap", type=float, default=0.5,
                    help="seconds between saved frames with detections (default 0.5)")
    ap.add_argument("--out", default=os.path.join(HERE, "dataset", "drone"))
    args = ap.parse_args()
    total = sum(extract(r, args.out, args.every, args.hit_gap) for r in args.recordings)
    print("%d images in all" % total)


if __name__ == "__main__":
    main()
