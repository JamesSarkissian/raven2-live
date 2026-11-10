#!/usr/bin/env python3
"""Train the road-damage detector (potholes and cracks) from scratch.

Run it with the TensorFlow environment:
  ~/PotholeRecognitionModel/tf_env/bin/python train_detector.py

Options:
  --data ~/Downloads/RoadDamage   merged dataset from build_dataset.py
  --epochs 60                     passes over the training images (~6.5 min each on an M4)
  --out models/road_damage        where checkpoints and the model go
  --input 256                     image size the model sees (320 is sharper, ~1.5x slower)
  --limit N                       use only N training images (quick tests)
  --init models/road_damage/best.keras
                                  start from an already-trained model instead of
                                  random weights (retraining with new drone data)
Stop it any time with Ctrl+C; running it again resumes from the last epoch.

Writes to --out:
  best.keras     model with the best validation score so far (use this one)
  last.keras     model after the latest epoch (for resuming)
  state.json     epoch, best score, settings
  history.csv    loss and validation scores per epoch
  report.txt     final test-set results (and on test_drone/, the held-out drone
                 clips, when the data has it: see build_drone_dataset.py)
"""
import argparse
import collections
import csv
import json
import os
import random
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# the input size must be known before the model module loads
for i, a in enumerate(sys.argv):
    if a == "--input" and i + 1 < len(sys.argv):
        os.environ["DAMAGE_INPUT"] = sys.argv[i + 1]
    elif a.startswith("--input="):
        os.environ["DAMAGE_INPUT"] = a.split("=", 1)[1]
import damage_model as dm  # noqa: E402

import tensorflow as tf  # noqa: E402


# ---- data ----------------------------------------------------------------------

def load_split(root, split):
    """[(image path, boxes (N,4) float32, classes (N,) int)] for one split."""
    by_img = collections.OrderedDict()
    with open(os.path.join(root, split, "_annotations.csv")) as f:
        for r in csv.DictReader(f):
            item = by_img.setdefault(r["filename"], ([], []))
            if r["class"] in dm.CLASSES:
                item[0].append([float(r[k]) for k in ("xmin", "ymin", "xmax", "ymax")])
                item[1].append(dm.CLASSES.index(r["class"]))
    return [(os.path.join(root, split, f), np.array(b, np.float32).reshape(-1, 4), np.array(c, np.int32))
            for f, (b, c) in by_img.items()]


def read_rgb(path):
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise IOError("cannot read " + path)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def augment(img, boxes, classes, rng):
    """Random crop/zoom, flip, colour, blur and sensor noise, so ~8k images
    look like many more and the model copes with the drone's grainy video."""
    h, w = img.shape[:2]
    # crop 50-100% of the area with a little aspect change; keep boxes whose
    # centre stays inside, clipped to the crop
    for _ in range(10):
        area = rng.uniform(0.5, 1.0) * w * h
        aspect = rng.uniform(0.75, 1.33)
        cw, ch = int(round(np.sqrt(area * aspect))), int(round(np.sqrt(area / aspect)))
        if cw <= w and ch <= h:
            break
    else:
        cw, ch = w, h
    x0, y0 = rng.randint(0, w - cw), rng.randint(0, h - ch)
    img = img[y0:y0 + ch, x0:x0 + cw]
    if len(boxes):
        cx, cy = (boxes[:, 0] + boxes[:, 2]) / 2, (boxes[:, 1] + boxes[:, 3]) / 2
        keep = (cx >= x0) & (cx < x0 + cw) & (cy >= y0) & (cy < y0 + ch)
        boxes, classes = boxes[keep] - [x0, y0, x0, y0], classes[keep]
        boxes = np.clip(boxes, 0, [cw, ch, cw, ch])
    if rng.random() < 0.5:
        img = img[:, ::-1]
        if len(boxes):
            boxes = np.stack([cw - boxes[:, 2], boxes[:, 1], cw - boxes[:, 0], boxes[:, 3]], 1)
    img = np.ascontiguousarray(img)
    # colour: brightness/contrast, saturation, slight hue shift
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[..., 0] = (hsv[..., 0] + rng.uniform(-6, 6)) % 180
    hsv[..., 1] *= rng.uniform(0.6, 1.4)
    img = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2RGB).astype(np.float32)
    img = img * rng.uniform(0.7, 1.3) + rng.uniform(-30, 30)
    if rng.random() < 0.2:
        img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.5, 1.5))
    if rng.random() < 0.3:      # grain like the drone camera in low light
        img = img + np.random.normal(0, rng.uniform(4, 14), img.shape)
    return np.clip(img, 0, 255), boxes, classes


def make_example(item, train, rng):
    path, boxes, classes = item
    img = read_rgb(path)
    if train:
        img, boxes, classes = augment(img.astype(np.float32), boxes.copy(), classes.copy(), rng)
    h, w = img.shape[:2]
    img = cv2.resize(img.astype(np.float32), (dm.INPUT, dm.INPUT), interpolation=cv2.INTER_AREA)
    if len(boxes):
        boxes = boxes * [dm.INPUT / w, dm.INPUT / h, dm.INPUT / w, dm.INPUT / h]
    return img.astype(np.float32), dm.encode(boxes, classes)


def dataset(items, batch, train, seed=0):
    rng = random.Random(seed)

    def gen(i):
        return make_example(items[int(i)], train, rng)

    ds = tf.data.Dataset.range(len(items))
    if train:
        ds = ds.shuffle(len(items), reshuffle_each_iteration=True)
    ds = ds.map(lambda i: tf.numpy_function(gen, [i], (tf.float32, tf.float32)),
                num_parallel_calls=tf.data.AUTOTUNE, deterministic=not train)
    ds = ds.map(lambda x, y: (tf.ensure_shape(x, (dm.INPUT, dm.INPUT, 3)),
                              tf.ensure_shape(y, (dm.OUT, dm.OUT, dm.NUM_CLASSES + 5))))
    return ds.batch(batch, drop_remainder=train).prefetch(tf.data.AUTOTUNE)


# ---- evaluation ----------------------------------------------------------------

def predict_split(model, items, batch=16):
    """Run the model on a split -> (predictions, truths) dicts for dm.evaluate."""
    preds, truths = {}, {}
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        imgs, sizes = [], []
        for path, boxes, classes in chunk:
            img = read_rgb(path)
            sizes.append(img.shape[:2])
            imgs.append(cv2.resize(img, (dm.INPUT, dm.INPUT), interpolation=cv2.INTER_AREA).astype(np.float32))
        outs = model(np.stack(imgs), training=False).numpy()
        for (path, boxes, classes), (h, w), out in zip(chunk, sizes, outs):
            preds[path] = dm.decode(out, w, h, threshold=0.05)
            truths[path] = [(dm.CLASSES[c],) + tuple(b) for b, c in zip(boxes, classes)]
    return preds, truths


def score_line(res):
    return "  ".join("%s AP %.3f (P %.2f R %.2f, %d)" % (k, v["ap"], v["precision"], v["recall"], v["truths"])
                     for k, v in res.items())


class EpochEnd(tf.keras.callbacks.Callback):
    """Validation AP every few epochs, best-model saving, resume state, ETA."""

    def __init__(self, out, valid, state, total_epochs, every):
        super().__init__()
        self.out, self.valid, self.state = out, valid, state
        self.total, self.every = total_epochs, every
        self.t0 = time.time()
        self.first = state["epoch"]

    def on_epoch_end(self, epoch, logs=None):
        done = epoch + 1
        row = {"epoch": done, "loss": logs.get("loss"), "val_loss": logs.get("val_loss"),
               "lr": float(tf.keras.backend.get_value(self.model.optimizer.learning_rate))}
        if done % self.every == 0 or done == self.total:
            res = dm.evaluate(*predict_split(self.model, self.valid))
            row["val_map"] = dm.mean_ap(res)
            row.update({"val_ap_" + k: v["ap"] for k, v in res.items()})
            print("  validation: mAP@0.5 %.3f | %s" % (row["val_map"], score_line(res)), flush=True)
            if row["val_map"] > self.state["best_map"]:
                self.state["best_map"], self.state["best_epoch"] = row["val_map"], done
                self.model.save(os.path.join(self.out, "best.keras"))
                print("  saved best.keras (epoch %d)" % done, flush=True)
        self.model.save(os.path.join(self.out, "last.keras"))
        self.state["epoch"] = done
        with open(os.path.join(self.out, "state.json"), "w") as f:
            json.dump(self.state, f, indent=1)
        hist = os.path.join(self.out, "history.csv")
        new = not os.path.exists(hist)
        with open(hist, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["epoch", "loss", "val_loss", "lr", "val_map", "val_ap_pothole", "val_ap_crack"])
            if new:
                w.writeheader()
            w.writerow(row)
        per = (time.time() - self.t0) / (done - self.first)
        left = per * (self.total - done)
        print("  epoch %d/%d took %.0f s; about %dh %02dm left" % (done, self.total, per, left // 3600, left % 3600 // 60), flush=True)


# ---- main ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Train the road-damage detector from scratch")
    ap.add_argument("--data", default="~/Downloads/RoadDamage")
    ap.add_argument("--out", default=os.path.join(HERE, "models", "road_damage"))
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--eval-every", type=int, default=5)
    ap.add_argument("--input", type=int, default=dm.INPUT, help="image size the model sees")
    ap.add_argument("--limit", type=int, default=0, help="use only this many training images")
    ap.add_argument("--init", help="start from this trained model instead of random weights")
    args = ap.parse_args()
    data = os.path.expanduser(args.data)
    os.makedirs(args.out, exist_ok=True)

    train = load_split(data, "train")
    valid = load_split(data, "valid")
    if args.limit:
        for split in (train, valid):
            random.Random(1).shuffle(split)     # mix sources so a sample has both classes
        train, valid = train[:args.limit], valid[:max(32, args.limit // 5)]
    steps = len(train) // args.batch
    print("training images %d, validation %d, %d steps per epoch, input %dx%d" % (
        len(train), len(valid), steps, dm.INPUT, dm.INPUT), flush=True)

    state_path = os.path.join(args.out, "state.json")
    state = {"epoch": 0, "best_map": -1.0, "best_epoch": 0, "epochs": args.epochs,
             "classes": list(dm.CLASSES), "input": dm.INPUT}
    if os.path.exists(state_path) and os.path.exists(os.path.join(args.out, "last.keras")):
        with open(state_path) as f:
            state.update(json.load(f), epochs=args.epochs)
        if state.get("input", dm.INPUT) != dm.INPUT:
            sys.exit("this checkpoint was trained at --input %d; use that, or a new --out folder" % state["input"])
        model = tf.keras.models.load_model(os.path.join(args.out, "last.keras"), compile=False)
        print("resuming after epoch %d (best validation mAP %.3f at epoch %d)" % (
            state["epoch"], state["best_map"], state["best_epoch"]), flush=True)
    elif args.init:
        model = tf.keras.models.load_model(os.path.expanduser(args.init), compile=False)
        if int(model.input_shape[1]) != dm.INPUT:
            sys.exit("%s was trained at --input %d; use that" % (args.init, model.input_shape[1]))
        state["init"] = os.path.abspath(os.path.expanduser(args.init))
        print("starting from %s (%.1f M parameters)" % (args.init, dm.param_count(model) / 1e6), flush=True)
    else:
        model = dm.build_model()
        print("new model, %.1f M parameters, random start (no pre-trained weights)" % (
            dm.param_count(model) / 1e6), flush=True)

    # warm up for 2 epochs, then cosine decay; the schedule continues on resume
    total = args.epochs * steps
    warm = min(2 * steps, total // 10)
    sched = tf.keras.optimizers.schedules.CosineDecay(args.lr * 0.1, max(1, total - warm), alpha=0.02,
                                                      warmup_target=args.lr, warmup_steps=warm)
    opt = tf.keras.optimizers.AdamW(sched, weight_decay=1e-4, clipnorm=10.0)
    model.compile(optimizer=opt, loss=dm.make_loss())
    if state["epoch"]:
        opt.iterations.assign(state["epoch"] * steps)

    if state["epoch"] < args.epochs:
        try:
            model.fit(dataset(train, args.batch, True, seed=state["epoch"]),
                      validation_data=dataset(valid, args.batch, False),
                      epochs=args.epochs, initial_epoch=state["epoch"], steps_per_epoch=steps, verbose=1,
                      callbacks=[EpochEnd(args.out, valid, state, args.epochs, args.eval_every)])
        except KeyboardInterrupt:
            print("\nstopped; run the same command again to resume from epoch %d" % state["epoch"])
            return 1

    # final check on the test images the model never saw
    best = os.path.join(args.out, "best.keras")
    model = tf.keras.models.load_model(best if os.path.exists(best) else os.path.join(args.out, "last.keras"),
                                       compile=False)
    test = load_split(data, "test")
    if args.limit:
        random.Random(1).shuffle(test)
        test = test[:max(32, args.limit // 5)]
    res = dm.evaluate(*predict_split(model, test))
    loose = dm.evaluate(*predict_split(model, test), iou_thr=0.3)
    lines = ["Test set: %d images" % len(test),
             "mAP@0.5 %.3f | %s" % (dm.mean_ap(res), score_line(res)),
             "mAP@0.3 %.3f | %s" % (dm.mean_ap(loose), score_line(loose)),
             "(P/R = precision/recall at score 0.3; @0.3 is a looser box match, fairer for long cracks)",
             "best epoch %d of %d" % (state["best_epoch"], state["epoch"])]
    if os.path.exists(os.path.join(data, "test_drone", "_annotations.csv")) and not args.limit:
        dtest = load_split(data, "test_drone")
        pred = predict_split(model, dtest)
        res, loose = dm.evaluate(*pred), dm.evaluate(*pred, iou_thr=0.3)
        lines += ["", "Held-out drone clips (test_drone): %d sections" % len(dtest),
                  "mAP@0.5 %.3f | %s" % (dm.mean_ap(res), score_line(res)),
                  "mAP@0.3 %.3f | %s" % (dm.mean_ap(loose), score_line(loose))]
    print("\n" + "\n".join(lines))
    with open(os.path.join(args.out, "report.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
