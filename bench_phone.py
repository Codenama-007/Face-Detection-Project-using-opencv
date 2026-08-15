"""
bench_phone.py — measures phone-detection precision and recall on real
labelled data (COCO val2017).

Two populations:
  * 214 images that genuinely contain a phone  -> measures RECALL
  * 489 images containing rectangular distractors (book, remote, laptop,
    keyboard, tv, mouse) but NO phone           -> measures FALSE POSITIVES

Every predicted phone box is attributed to whatever ground-truth object it
actually overlaps, so "it flags any rectangular thing as a phone" becomes a
concrete per-class count instead of an impression.

Usage:
    python bench_phone.py                      # default config sweep
    python bench_phone.py yolo11s.pt 640 0.5   # single config
"""
import json
import os
import sys
import time
from collections import Counter, defaultdict

import cv2
import numpy as np
from ultralytics import YOLO

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "datasets")
IMG_DIR = os.path.join(DATA, "val2017")
ANN = os.path.join(DATA, "annotations", "instances_val2017.json")

PHONE_CLASS = "cell phone"
DISTRACTORS = {"book", "remote", "laptop", "keyboard", "tv", "mouse"}
COCO_PHONE_ID = 67          # ultralytics COCO index for 'cell phone'

MAX_PHONE_IMAGES = 214
MAX_DISTRACT_IMAGES = 250


def iou(a, b):
    ax1, ay1, aw, ah = a; ax2, ay2 = ax1 + aw, ay1 + ah
    bx1, by1, bw, bh = b; bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def load_sets():
    d = json.load(open(ANN))
    cats = {c["id"]: c["name"] for c in d["categories"]}
    by_img = defaultdict(list)
    for a in d["annotations"]:
        by_img[a["image_id"]].append((cats[a["category_id"]], a["bbox"]))
    files = {im["id"]: im["file_name"] for im in d["images"]}

    phone_imgs, distract_imgs = [], []
    for iid, anns in by_img.items():
        names = {n for n, _ in anns}
        if PHONE_CLASS in names:
            phone_imgs.append(iid)
        elif names & DISTRACTORS:
            distract_imgs.append(iid)
    return files, by_img, sorted(phone_imgs), sorted(distract_imgs)


def evaluate(model, imgsz, conf, files, by_img, phone_imgs, distract_imgs,
             verbose=True):
    tp = fn = 0
    fp_total = 0
    fp_by_class = Counter()
    fp_sizes = []
    detected_widths = []

    # ---- RECALL: images that really contain a phone ----
    for iid in phone_imgs[:MAX_PHONE_IMAGES]:
        path = os.path.join(IMG_DIR, files[iid])
        img = cv2.imread(path)
        if img is None:
            continue
        gt_phones = [b for n, b in by_img[iid] if n == PHONE_CLASS]
        preds = []
        for r in model(img, stream=True, verbose=False, imgsz=imgsz, conf=conf):
            for box in r.boxes:
                if int(box.cls[0]) != COCO_PHONE_ID:
                    continue
                x1, y1, x2, y2 = map(float, box.xyxy[0])
                preds.append((x1, y1, x2 - x1, y2 - y1))
        for g in gt_phones:
            if any(iou(p, g) >= 0.3 for p in preds):
                tp += 1
                detected_widths.append(g[2])
            else:
                fn += 1

    # ---- FALSE POSITIVES: images with NO phone at all ----
    for iid in distract_imgs[:MAX_DISTRACT_IMAGES]:
        path = os.path.join(IMG_DIR, files[iid])
        img = cv2.imread(path)
        if img is None:
            continue
        objs = by_img[iid]
        for r in model(img, stream=True, verbose=False, imgsz=imgsz, conf=conf):
            for box in r.boxes:
                if int(box.cls[0]) != COCO_PHONE_ID:
                    continue
                x1, y1, x2, y2 = map(float, box.xyxy[0])
                p = (x1, y1, x2 - x1, y2 - y1)
                fp_total += 1
                fp_sizes.append(p[2])
                best_n, best_i = "background", 0.0
                for n, b in objs:
                    v = iou(p, b)
                    if v > best_i:
                        best_n, best_i = n, v
                fp_by_class[best_n if best_i >= 0.2 else "background"] += 1

    recall = tp / max(tp + fn, 1)
    # precision proxy: true phones found vs all phone-labelled boxes emitted
    precision = tp / max(tp + fp_total, 1)
    if verbose:
        print(f"  recall      {recall*100:5.1f}%  ({tp} found / {tp+fn} real phones)")
        print(f"  FALSE POSITIVES on {MAX_DISTRACT_IMAGES} phone-free images: {fp_total}")
        if fp_by_class:
            top = ", ".join(f"{k}:{v}" for k, v in fp_by_class.most_common(6))
            print(f"  misfired on -> {top}")
        if detected_widths:
            print(f"  detected phone widths: median {np.median(detected_widths):.0f}px, "
                  f"min {min(detected_widths):.0f}px")
    return {"recall": recall, "tp": tp, "fn": fn, "fp": fp_total,
            "precision": precision, "fp_by_class": fp_by_class}


def main():
    if not os.path.isdir(IMG_DIR):
        print(f"{IMG_DIR} missing — val2017 images not extracted yet")
        return
    files, by_img, phone_imgs, distract_imgs = load_sets()
    print(f"phone images: {len(phone_imgs)}, phone-free distractor images: "
          f"{len(distract_imgs)} (using up to {MAX_DISTRACT_IMAGES})\n")

    if len(sys.argv) > 1:
        configs = [(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]))]
    else:
        configs = [
            ("yolo11n.pt", 480, 0.60),   # what the server currently runs
            ("yolo11n.pt", 640, 0.60),
            ("yolo11n.pt", 960, 0.60),
            ("yolo11s.pt", 640, 0.60),
            ("yolo11m.pt", 640, 0.60),
            ("yolo11m.pt", 960, 0.60),
        ]

    cache = {}
    for weights, imgsz, conf in configs:
        if weights not in cache:
            cache[weights] = YOLO(os.path.join(BASE, weights))
        print(f"=== {weights}  imgsz={imgsz}  conf={conf} ===")
        t0 = time.time()
        evaluate(cache[weights], imgsz, conf, files, by_img,
                 phone_imgs, distract_imgs)
        print(f"  ({time.time()-t0:.0f}s)\n")


if __name__ == "__main__":
    main()
