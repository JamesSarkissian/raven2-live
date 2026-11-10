#!/usr/bin/env python3
"""Mix your labelled drone frames into the training data for retraining.

    ~/PotholeRecognitionModel/tf_env/bin/python build_drone_dataset.py
        [--base ~/Downloads/RoadDamage] [--drone dataset/drone] [--out ~/Downloads/RoadDamageDrone]

Takes the images you marked "done" in label_tool.py and:
  - splits them into train / valid / test BY PLACE, so the same pothole or
    crack never appears in both training and testing: clips whose GPS tracks
    come within 15 m of each other stay together; clips with no GPS (flight
    data not connected) are each kept whole;
  - prepares them the way the live viewer sees them (detect_drone): the
    haze/glare correction, then left / middle / right square sections, with
    the boxes cut to each section;
  - repeats the drone training sections (--repeat, default 2) so a few hundred
    drone frames are not drowned out by the ~6,400 original images.
Output: the original train/valid/test (hard-linked, no extra disk space) with
the drone sections added, plus test_drone/ (drone test sections only) for the
before/after comparison, and drone_split.json saying which clip went where.
Your labels and recordings are only read, never changed.
"""
import argparse
import csv
import json
import math
import os
import random
import shutil
import sys

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from detect import enhance  # noqa: E402

FIELDS = ["filename", "width", "height", "class", "xmin", "ymin", "xmax", "ymax", "source", "subtype"]
NEAR_M = 15.0
MIN_SIDE = 12           # px: a box cut down to less than this by a section edge is dropped


def link(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def dist_m(a, b):
    k = math.pi / 180
    x = (b[1] - a[1]) * k * math.cos((a[0] + b[0]) / 2 * k)
    return 6371000 * math.hypot(x, (b[0] - a[0]) * k)


def load_drone(root):
    """{clip: [(image path, w, h, boxes, (lat, lon) or None)]} for images marked done."""
    clips = {}
    for clip in sorted(os.listdir(root)):
        folder = os.path.join(root, clip)
        lab = os.path.join(folder, "_labels.json")
        if not os.path.isfile(lab):
            continue
        with open(lab) as f:
            labels = json.load(f)
        where = {}
        if os.path.exists(os.path.join(folder, "frames.csv")):
            with open(os.path.join(folder, "frames.csv")) as f:
                for r in csv.DictReader(f):
                    if r.get("lat") and r.get("lon"):
                        where[r["filename"]] = (float(r["lat"]), float(r["lon"]))
        items = [(os.path.join(folder, n), it["w"], it["h"], it["boxes"], where.get(n))
                 for n, it in sorted(labels.items()) if it["status"] == "done"]
        if items:
            clips[clip] = items
    return clips


def group_clips(clips):
    """Clips that pass within NEAR_M of each other share a group."""
    names = list(clips)
    parent = {n: n for n in names}

    def find(n):
        while parent[n] != n:
            n = parent[n]
        return n
    pts = {n: [p for *_, p in clips[n] if p] for n in names}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if pts[a] and pts[b] and any(dist_m(p, q) < NEAR_M for p in pts[a][::2] for q in pts[b][::2]):
                parent[find(a)] = find(b)
    groups = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    return sorted(groups.values())


def choose_split(groups, clips, test_share, valid_share):
    """Try many shuffles; keep the one closest to the wanted shares that has
    potholes and cracks in every split."""
    def count(gs):
        frames = sum(len(clips[c]) for g in gs for c in g)
        cls = {k: sum(1 for g in gs for c in g for it in clips[c] for b in it[3] if b[0] == k)
               for k in ("pothole", "crack")}
        return frames, cls
    total = sum(len(v) for v in clips.values())
    best = None
    for seed in range(500):
        order = groups[:]
        random.Random(seed).shuffle(order)
        test, valid, train, n = [], [], [], 0
        for g in order:
            size = sum(len(clips[c]) for c in g)
            if n < test_share * total:
                test.append(g)
            elif n < (test_share + valid_share) * total:
                valid.append(g)
            else:
                train.append(g)
            n += size
        parts = [count(s) for s in (train, valid, test)]
        if any(min(cls.values()) == 0 for _, cls in parts):
            continue
        err = abs(parts[2][0] / total - test_share) + abs(parts[1][0] / total - valid_share)
        if best is None or err < best[0]:
            best = (err, train, valid, test)
    if best is None:
        sys.exit("could not split so every part has both potholes and cracks; label more clips")
    return best[1:]


def sections(path, w, h, boxes):
    """Haze-corrected left/middle/right squares (as detect_drone) with boxes cut to each."""
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    img = enhance(img)
    h, w = img.shape[:2]
    s = min(w, h)
    xs = sorted({0, (w - s) // 2, w - s})
    ys = sorted({0, (h - s) // 2, h - s})
    out = []
    for x in xs:
        for y in ys:
            cut = []
            for c, x0, y0, x1, y1 in boxes:
                a0, b0, a1, b1 = max(x0, x), max(y0, y), min(x1, x + s), min(y1, y + s)
                if a1 - a0 >= MIN_SIDE and b1 - b0 >= MIN_SIDE:
                    cut.append([c, a0 - x, b0 - y, a1 - x, b1 - y])
            out.append((img[y:y + s, x:x + s], s, cut, "%d_%d" % (x, y)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="~/Downloads/RoadDamage")
    ap.add_argument("--drone", default=os.path.join(HERE, "dataset", "drone"))
    ap.add_argument("--out", default="~/Downloads/RoadDamageDrone")
    ap.add_argument("--repeat", type=int, default=2, help="times each drone training section is used")
    ap.add_argument("--test", type=float, default=0.18, help="share of drone frames held out for testing")
    ap.add_argument("--valid", type=float, default=0.12, help="share of drone frames for validation")
    args = ap.parse_args()
    base, out = os.path.expanduser(args.base), os.path.expanduser(args.out)

    clips = load_drone(args.drone)
    if not clips:
        sys.exit("no images marked done in %s" % args.drone)
    groups = group_clips(clips)
    train_g, valid_g, test_g = choose_split(groups, clips, args.test, args.valid)

    if os.path.exists(out):
        shutil.rmtree(out)      # only ever a generated folder: rebuilt from scratch each time
    rows = {}
    for split in ("train", "valid", "test"):
        os.makedirs(os.path.join(out, split))
        with open(os.path.join(base, split, "_annotations.csv")) as f:
            rows[split] = [dict(r, source=r.get("source", ""), subtype=r.get("subtype", "")) for r in csv.DictReader(f)]
        for n in {r["filename"] for r in rows[split]}:
            link(os.path.join(base, split, n), os.path.join(out, split, n))
    os.makedirs(os.path.join(out, "test_drone"))
    rows["test_drone"] = []

    summary = {}
    for split, gs in (("train", train_g), ("valid", valid_g), ("test", test_g)):
        frames = boxes = 0
        for g in gs:
            for clip in g:
                for path, w, h, bx, _ in clips[clip]:
                    frames += 1
                    stem = os.path.splitext(os.path.basename(path))[0]
                    for img, s, cut, tag in sections(path, w, h, bx):
                        reps = args.repeat if split == "train" else 1
                        targets = [split] + (["test_drone"] if split == "test" else [])
                        for t in targets:
                            for k in range(reps):
                                name = "drone_%s_%s%s.jpg" % (stem, tag, "_r%d" % k if k else "")
                                dst = os.path.join(out, t, name)
                                if k == 0 and t == split:
                                    cv2.imwrite(dst, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
                                    first = dst
                                else:
                                    link(first, dst)
                                for c, x0, y0, x1, y1 in cut:
                                    rows[t].append({"filename": name, "width": s, "height": s, "class": c,
                                                    "xmin": x0, "ymin": y0, "xmax": x1, "ymax": y1,
                                                    "source": "drone", "subtype": ""})
                                if not cut:
                                    rows[t].append({"filename": name, "width": s, "height": s, "class": "",
                                                    "xmin": "", "ymin": "", "xmax": "", "ymax": "",
                                                    "source": "drone", "subtype": ""})
                                if k == 0 and t == split:
                                    boxes += len(cut)
        summary[split] = {"clips": sorted(c for g in gs for c in g), "frames": frames, "section_boxes": boxes}
    for split, rs in rows.items():
        with open(os.path.join(out, split, "_annotations.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rs)
    with open(os.path.join(out, "drone_split.json"), "w") as f:
        json.dump(summary, f, indent=1)

    for split in ("train", "valid", "test", "test_drone"):
        names = {r["filename"] for r in rows[split]}
        drone = {n for n in names if n.startswith("drone_")}
        print("%-10s %5d images (%d drone sections)" % (split, len(names), len(drone)))
    for split, s in summary.items():
        print("drone %-5s %3d frames from %d clips" % (split, s["frames"], len(s["clips"])))
    print("written to %s" % out)


if __name__ == "__main__":
    main()
