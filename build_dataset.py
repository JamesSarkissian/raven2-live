#!/usr/bin/env python3
"""Merge the Pothole dataset and RDD2022 folders into one training set.

Output (default ~/Downloads/RoadDamage) has train/, valid/, test/, each with
images and an _annotations.csv in the same columns as the Pothole set:
  filename,width,height,class,xmin,ymin,xmax,ymax,source,subtype
class is "pothole" or "crack" (blank = image with no damage boxes).
subtype keeps RDD2022's code: D00 lengthwise crack, D10 crosswise crack,
D20 alligator crack, D40 pothole.

Images are hard-linked, not copied, so the merged set takes almost no extra
disk space and the original folders are left untouched.

Usage: ./build_dataset.py [--pothole ~/Downloads/Pothole] [--rdd ~/Downloads/RDD2022]
"""
import argparse
import collections
import csv
import os
import random
import shutil
import xml.etree.ElementTree as ET

CLASS_MAP = {"D00": "crack", "D10": "crack", "D20": "crack", "D40": "pothole"}
FIELDS = ["filename", "width", "height", "class", "xmin", "ymin", "xmax", "ymax", "source", "subtype"]
SPLITS = ("train", "valid", "test")


def link(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)      # different disk: fall back to copying


def pothole_rows(root):
    """Your Roboflow pothole set, already split."""
    for split in SPLITS:
        with open(os.path.join(root, split, "_annotations.csv")) as f:
            for r in csv.DictReader(f):
                yield split, os.path.join(root, split, r["filename"]), dict(
                    r, source="pothole", subtype="D40" if r["class"] else "")


def rdd_images(root):
    """(country, image path, [boxes]) for every labelled RDD2022 image."""
    for country in sorted(os.listdir(root)):
        xml_dir = os.path.join(root, country, "train", "annotations", "xmls")
        img_dir = os.path.join(root, country, "train", "images")
        if not os.path.isdir(xml_dir):
            continue
        for name in sorted(os.listdir(xml_dir)):
            if not name.endswith(".xml"):
                continue
            tree = ET.parse(os.path.join(xml_dir, name))
            img = os.path.join(img_dir, tree.findtext("filename") or name[:-4] + ".jpg")
            if not os.path.exists(img):
                continue
            size = tree.find("size")
            w, h = int(float(size.findtext("width"))), int(float(size.findtext("height")))
            boxes = []
            for obj in tree.findall("object"):
                code = (obj.findtext("name") or "").strip()
                bb = obj.find("bndbox")
                if code not in CLASS_MAP or bb is None:
                    continue    # other damage types (repairs, manholes...) are left out
                x0, y0, x1, y1 = (int(float(bb.findtext(k))) for k in ("xmin", "ymin", "xmax", "ymax"))
                x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
                if x1 - x0 >= 2 and y1 - y0 >= 2:
                    boxes.append((CLASS_MAP[code], x0, y0, x1, y1, code))
            yield country, img, w, h, boxes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pothole", default="~/Downloads/Pothole")
    ap.add_argument("--rdd", default="~/Downloads/RDD2022")
    ap.add_argument("--out", default="~/Downloads/RoadDamage")
    ap.add_argument("--valid", type=float, default=0.1, help="share of RDD images for validation")
    ap.add_argument("--test", type=float, default=0.1, help="share of RDD images for testing")
    args = ap.parse_args()
    pothole, rdd, out = (os.path.expanduser(p) for p in (args.pothole, args.rdd, args.out))
    if os.path.exists(out):
        raise SystemExit("%s already exists; delete it first to rebuild" % out)
    for s in SPLITS:
        os.makedirs(os.path.join(out, s))
    rows = {s: [] for s in SPLITS}

    for split, src, r in pothole_rows(pothole):
        name = "ph_" + r["filename"]
        dst = os.path.join(out, split, name)
        if not os.path.exists(dst):
            link(src, dst)
        rows[split].append(dict(r, filename=name))

    # RDD2022 has labels only for its train part: split it here, the same way every run
    images = list(rdd_images(rdd))
    random.Random(2022).shuffle(images)
    n_test, n_valid = int(len(images) * args.test), int(len(images) * args.valid)
    for i, (country, src, w, h, boxes) in enumerate(images):
        split = "test" if i < n_test else "valid" if i < n_test + n_valid else "train"
        name = "rdd_%s_%s" % (country.lower(), os.path.basename(src))
        link(src, os.path.join(out, split, name))
        base = {"filename": name, "width": w, "height": h, "source": "rdd_" + country.lower()}
        if boxes:
            for cls, x0, y0, x1, y1, code in boxes:
                rows[split].append(dict(base, **{"class": cls, "xmin": x0, "ymin": y0,
                                                 "xmax": x1, "ymax": y1, "subtype": code}))
        else:
            rows[split].append(dict(base, **{"class": "", "xmin": "", "ymin": "", "xmax": "", "ymax": "", "subtype": ""}))

    print("%-6s %7s %9s %8s %8s %11s" % ("split", "images", "no damage", "potholes", "cracks", "crack types"))
    for s in SPLITS:
        with open(os.path.join(out, s, "_annotations.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows[s])
        imgs = collections.defaultdict(list)
        for r in rows[s]:
            imgs[r["filename"]].append(r["class"])
        cls = collections.Counter(r["class"] for r in rows[s] if r["class"])
        sub = collections.Counter(r["subtype"] for r in rows[s] if r["class"] == "crack")
        print("%-6s %7d %9d %8d %8d   %s" % (s, len(imgs), sum(1 for c in imgs.values() if not any(c)),
                                         cls["pothole"], cls["crack"],
                                         " ".join("%s:%d" % kv for kv in sorted(sub.items()))))
    print("by source:", dict(collections.Counter(
        r["source"] for s in SPLITS for r in {x["filename"]: x for x in rows[s]}.values())))
    print("written to", out)


if __name__ == "__main__":
    main()
