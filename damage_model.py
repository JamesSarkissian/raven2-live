"""Road-damage detector trained from scratch: model, training targets, loss,
decoding and evaluation.

It is a small CenterNet-style detector ("Objects as Points", Zhou et al. 2019):
for each class it predicts a heatmap that peaks at the centre of every pothole
or crack, plus the box width/height and a sub-pixel offset at that point. No
pre-trained weights are used; every layer starts from random values.

Input: RGB image resized to INPUT x INPUT, values 0-255.
Output: one tensor (INPUT/4, INPUT/4, NUM_CLASSES + 4):
  [..., :C]    heatmap logits per class
  [..., C:C+2] box width, height in output cells (1 cell = 4 input pixels)
  [..., C+2:]  centre offset within the cell (0..1)
Only standard Keras layers are used, so the saved .keras file loads without
this module (tf.keras.models.load_model(path, compile=False)).
"""
import math

import numpy as np

CLASSES = ("pothole", "crack")
NUM_CLASSES = len(CLASSES)
# input size; train_detector.py --input changes it (the saved model remembers its own)
INPUT = int(__import__('os').environ.get('DAMAGE_INPUT', 256))
STRIDE = 4
OUT = INPUT // STRIDE
# potholes are rarer than cracks in the data (about 1:7) and matter more
CLASS_WEIGHTS = (2.0, 1.0)


# ---- model -------------------------------------------------------------------

def build_model(width=1.0):
    import tensorflow as tf
    L = tf.keras.layers
    c = lambda n: max(8, int(n * width))

    def conv(x, n, k=3, s=1):
        x = L.Conv2D(n, k, strides=s, padding="same", use_bias=False)(x)
        x = L.BatchNormalization()(x)
        return L.Activation("swish")(x)

    def block(x, n, s=1):
        """Residual block of two 3x3 convs. (Depthwise convs are lighter on
        paper but train 5-8x slower on a Mac CPU in TensorFlow.)"""
        y = conv(x, n, 3, s)
        y = L.Conv2D(n, 3, padding="same", use_bias=False)(y)
        y = L.BatchNormalization()(y)
        if s == 1 and x.shape[-1] == n:
            y = L.Add()([x, y])
        return L.Activation("swish")(y)

    def stage(x, n, s, repeat):
        x = block(x, n, s)
        for _ in range(repeat - 1):
            x = block(x, n)
        return x

    inp = L.Input((INPUT, INPUT, 3), name="image")
    x = L.Rescaling(1 / 255.0)(inp)
    x = conv(x, c(16), 3, 2)                 # /2
    s4 = stage(x, c(32), 2, 1)               # /4
    s8 = stage(s4, c(64), 2, 2)              # /8
    s16 = stage(s8, c(96), 2, 2)             # /16
    s32 = stage(s16, c(160), 2, 2)           # /32
    # top-down feature pyramid back to /4, where the heatmap lives
    p = conv(s32, c(64), 1)
    for skip in (s16, s8, s4):
        p = L.UpSampling2D(interpolation="bilinear")(p)
        p = L.Add()([p, conv(skip, c(64), 1)])
        p = conv(p, c(64))
    head = lambda n, bias="zeros": L.Conv2D(n, 1, bias_initializer=bias)(conv(p, c(48)))
    # start heatmaps near 0.1 probability, the usual CenterNet initialisation
    heat = head(NUM_CLASSES, tf.keras.initializers.Constant(-2.19))
    wh = head(2)
    off = head(2)
    out = L.Concatenate(name="detections")([heat, wh, off])
    return tf.keras.Model(inp, out, name="road_damage_centernet")


# ---- training targets ----------------------------------------------------------

def encode(boxes, classes):
    """boxes: (N, 4) x0, y0, x1, y1 in INPUT pixels. Returns (OUT, OUT, C + 5):
    heatmaps, wh, offset and a mask marking box centres."""
    t = np.zeros((OUT, OUT, NUM_CLASSES + 5), np.float32)
    for (x0, y0, x1, y1), k in zip(boxes, classes):
        w, h = (x1 - x0) / STRIDE, (y1 - y0) / STRIDE
        if w < 0.5 or h < 0.5:
            continue
        cx, cy = (x0 + x1) / 2 / STRIDE, (y0 + y1) / 2 / STRIDE
        ix, iy = min(int(cx), OUT - 1), min(int(cy), OUT - 1)
        # elliptical Gaussian: long cracks get long blobs instead of a round one
        sx, sy = max(w / 6, 0.8), max(h / 6, 0.8)
        rx, ry = int(3 * sx) + 1, int(3 * sy) + 1
        xs = np.arange(max(0, ix - rx), min(OUT, ix + rx + 1))
        ys = np.arange(max(0, iy - ry), min(OUT, iy + ry + 1))
        g = np.exp(-((xs[None, :] - ix) ** 2) / (2 * sx * sx) - ((ys[:, None] - iy) ** 2) / (2 * sy * sy))
        region = t[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1, k]
        np.maximum(region, g, out=region)
        t[iy, ix, k] = 1.0
        t[iy, ix, NUM_CLASSES:NUM_CLASSES + 2] = (w, h)
        t[iy, ix, NUM_CLASSES + 2:NUM_CLASSES + 4] = (cx - ix, cy - iy)
        t[iy, ix, NUM_CLASSES + 4] = 1.0
    return t


# ---- loss ----------------------------------------------------------------------

def make_loss(size_weight=0.1, offset_weight=1.0):
    import tensorflow as tf
    cw = tf.constant(CLASS_WEIGHTS, tf.float32)
    C = NUM_CLASSES

    def loss(y_true, y_pred):
        gt, p = y_true[..., :C], tf.clip_by_value(tf.sigmoid(y_pred[..., :C]), 1e-4, 1 - 1e-4)
        mask = y_true[..., C + 4:C + 5]
        n = tf.maximum(tf.reduce_sum(mask), 1.0)
        pos = tf.cast(gt >= 0.999, tf.float32)
        # penalty-reduced focal loss on the heatmaps
        pos_l = tf.math.log(p) * tf.square(1 - p) * pos
        neg_l = tf.math.log(1 - p) * tf.square(p) * tf.pow(1 - gt, 4) * (1 - pos)
        heat = -tf.reduce_sum((pos_l + neg_l) * cw) / n
        wh = tf.reduce_sum(tf.abs(y_pred[..., C:C + 2] - y_true[..., C:C + 2]) * mask) / n
        off = tf.reduce_sum(tf.abs(y_pred[..., C + 2:C + 4] - y_true[..., C + 2:C + 4]) * mask) / n
        return heat + size_weight * wh + offset_weight * off

    return loss


# ---- decoding ------------------------------------------------------------------

def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def decode(out, image_w, image_h, threshold=0.3, top_k=100):
    """One model output (OUT, OUT, C+4) -> [(class_name, score, x0, y0, x1, y1)]
    in the original image's pixels."""
    n_out = out.shape[0]                 # works for any input size the model was built with
    size = n_out * STRIDE
    heat = sigmoid(out[..., :NUM_CLASSES])
    # a peak is a cell that is the highest in its 3x3 neighbourhood
    pad = np.pad(heat, ((1, 1), (1, 1), (0, 0)), constant_values=0)
    neigh = np.max(np.stack([pad[dy:dy + n_out, dx:dx + n_out] for dy in range(3) for dx in range(3)]), axis=0)
    peaks = np.where((heat >= neigh) & (heat >= threshold))
    order = np.argsort(-heat[peaks])[:top_k]
    sx, sy = image_w / size, image_h / size
    dets = []
    for i in order:
        y, x, k = peaks[0][i], peaks[1][i], peaks[2][i]
        w, h = out[y, x, NUM_CLASSES:NUM_CLASSES + 2]
        ox, oy = out[y, x, NUM_CLASSES + 2:NUM_CLASSES + 4]
        cx, cy = (x + ox) * STRIDE, (y + oy) * STRIDE
        w, h = abs(w) * STRIDE, abs(h) * STRIDE
        dets.append((CLASSES[k], float(heat[y, x, k]),
                     max(0.0, (cx - w / 2) * sx), max(0.0, (cy - h / 2) * sy),
                     min(image_w, (cx + w / 2) * sx), min(image_h, (cy + h / 2) * sy)))
    return dets


# ---- evaluation ----------------------------------------------------------------

def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def evaluate(predictions, truths, iou_thr=0.5, score_thr=0.3):
    """predictions/truths: {image: [(class, score?, x0, y0, x1, y1)]}.
    Returns {class: {ap, precision, recall, truths}} (AP = area under the
    precision-recall curve, all detections; precision/recall at score_thr)."""
    result = {}
    for cls in CLASSES:
        gts = {img: [b[-4:] for b in boxes if b[0] == cls] for img, boxes in truths.items()}
        n_gt = sum(len(v) for v in gts.values())
        dets = sorted(((d[1], img, d[2:]) for img, ds in predictions.items() for d in ds if d[0] == cls),
                      key=lambda d: -d[0])
        used = {img: [False] * len(v) for img, v in gts.items()}
        tp, fp, scores = [], [], []
        for score, img, box in dets:
            best, j = 0.0, -1
            for i, g in enumerate(gts.get(img, [])):
                o = iou(box, g)
                if o > best and not used[img][i]:
                    best, j = o, i
            hit = best >= iou_thr
            if hit:
                used[img][j] = True
            tp.append(1 if hit else 0)
            fp.append(0 if hit else 1)
            scores.append(score)
        tp_c, fp_c = np.cumsum(tp), np.cumsum(fp)
        rec = tp_c / max(n_gt, 1)
        prec = tp_c / np.maximum(tp_c + fp_c, 1)
        # all-point interpolated average precision (as in PASCAL VOC 2010+)
        ap = 0.0
        if len(rec):
            mrec = np.concatenate([[0], rec, [1]])
            mpre = np.concatenate([[0], prec, [0]])
            for i in range(len(mpre) - 2, -1, -1):
                mpre[i] = max(mpre[i], mpre[i + 1])
            idx = np.where(mrec[1:] != mrec[:-1])[0]
            ap = float(np.sum((mrec[idx + 1] - mrec[idx]) * mpre[idx + 1]))
        k = sum(1 for s in scores if s >= score_thr)
        result[cls] = {"ap": ap, "truths": n_gt,
                       "precision": float(prec[k - 1]) if k else 0.0,
                       "recall": float(rec[k - 1]) if k else 0.0}
    return result


def mean_ap(result):
    return float(np.mean([r["ap"] for r in result.values()]))


def param_count(model):
    return int(sum(math.prod(w.shape) for w in model.trainable_weights))
