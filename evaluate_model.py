#!/usr/bin/env python3
"""Score a trained model on one part of a dataset (the "before" number).

    .venv/bin/python evaluate_model.py
        [--model models/road_damage/best.keras] [--data ~/Downloads/RoadDamageDrone] [--split test_drone]

Prints the same scores as the end of training: AP per class and mAP, with
precision/recall at score 0.3, at the strict (0.5) and looser (0.3) box match.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default=os.path.join(HERE, "models", "road_damage", "best.keras"))
    ap.add_argument("--data", default="~/Downloads/RoadDamageDrone")
    ap.add_argument("--split", default="test_drone")
    args = ap.parse_args()
    import tensorflow as tf
    import damage_model as dm
    from train_detector import load_split, predict_split, score_line
    model = tf.keras.models.load_model(os.path.expanduser(args.model), compile=False)
    items = load_split(os.path.expanduser(args.data), args.split)
    pred = predict_split(model, items)
    res, loose = dm.evaluate(*pred), dm.evaluate(*pred, iou_thr=0.3)
    print("%s on %s/%s: %d images" % (args.model, args.data, args.split, len(items)))
    print("mAP@0.5 %.3f | %s" % (dm.mean_ap(res), score_line(res)))
    print("mAP@0.3 %.3f | %s" % (dm.mean_ap(loose), score_line(loose)))


if __name__ == "__main__":
    main()
