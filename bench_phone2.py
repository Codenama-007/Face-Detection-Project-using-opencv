"""
bench_phone2.py — confidence sweep + person-ROI detection.

The baseline sweep showed the real defect is RECALL (79% of phones missed),
not false positives. This measures:
  1. the precision/recall trade-off across confidence thresholds, and
  2. whether cropping each person and detecting inside that crop recovers
     small / distant / partially-held phones.

Cropping helps because a phone that is 20px wide in a 960px frame becomes
~100px wide once a 200px-wide person box is upscaled to 640 — the detector
sees far more pixels on the object.
"""
import json
import os
from collections import Counter, defaultdict

import cv2
import numpy as np
from ultralytics import YOLO

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "datasets")
IMG_DIR = os.path.join(DATA, "val2017")
ANN = os.path.join(DATA, "annotations", "instances_val2017.json")

PHONE = "cell phone"
DISTRACTORS = {"book", "remote", "laptop", "keyboard", "tv", "mouse"}
PHONE_ID, PERSON_ID = 67, 0
# Kept modest so a full sweep finishes in minutes; raise for a final check.
N_PHONE_IMGS = int(os.environ.get("N_PHONE", 120))
N_DISTRACT_IMGS = int(os.environ.get("N_DISTRACT", 120))


def iou(a, b):
    ax1, ay1, aw, ah = a; ax2, ay2 = ax1 + aw, ay1 + ah
    bx1, by1, bw, bh = b; bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    u = aw * ah + bw * bh - inter
    return inter / u if u > 0 else 0.0


def load():
    d = json.load(open(ANN))
    cats = {c["id"]: c["name"] for c in d["categories"]}
    by_img = defaultdict(list)
    for a in d["annotations"]:
        by_img[a["image_id"]].append((cats[a["category_id"]], a["bbox"]))
    files = {im["id"]: im["file_name"] for im in d["images"]}
    ph, di = [], []
    for iid, anns in by_img.items():
        names = {n for n, _ in anns}
        if PHONE in names:
            ph.append(iid)
        elif names & DISTRACTORS:
            di.append(iid)
    return files, by_img, sorted(ph), sorted(di)


def detect_full(model, img, imgsz, conf):
    """Plain whole-frame detection."""
    out = []
    for r in model(img, stream=True, verbose=False, imgsz=imgsz, conf=conf):
        for b in r.boxes:
            if int(b.cls[0]) == PHONE_ID:
                x1, y1, x2, y2 = map(float, b.xyxy[0])
                out.append((x1, y1, x2 - x1, y2 - y1, float(b.conf[0])))
    return out


def detect_roi(model, img, imgsz, conf, person_model=None, pconf=0.35,
               roi_imgsz=640):
    """Detect persons, then re-detect phones inside each upscaled person crop.

    Returns boxes in original-image coordinates.
    """
    H, W = img.shape[:2]
    pm = person_model or model
    persons = []
    for r in pm(img, stream=True, verbose=False, imgsz=imgsz, conf=pconf):
        for b in r.boxes:
            if int(b.cls[0]) == PERSON_ID:
                x1, y1, x2, y2 = map(int, b.xyxy[0])
                persons.append((x1, y1, x2, y2))

    out = list(detect_full(model, img, imgsz, conf))   # keep whole-frame hits
    for (x1, y1, x2, y2) in persons:
        # pad the crop: a concealed phone is often just outside the torso box
        pw, ph_ = x2 - x1, y2 - y1
        px = int(pw * 0.15); py = int(ph_ * 0.15)
        cx1, cy1 = max(0, x1 - px), max(0, y1 - py)
        cx2, cy2 = min(W, x2 + px), min(H, y2 + py)
        if cx2 - cx1 < 32 or cy2 - cy1 < 32:
            continue
        crop = img[cy1:cy2, cx1:cx2]
        for r in model(crop, stream=True, verbose=False, imgsz=roi_imgsz, conf=conf):
            for b in r.boxes:
                if int(b.cls[0]) != PHONE_ID:
                    continue
                bx1, by1, bx2, by2 = map(float, b.xyxy[0])
                out.append((cx1 + bx1, cy1 + by1, bx2 - bx1, by2 - by1,
                            float(b.conf[0])))
    return out


def nms_boxes(boxes, thr=0.5):
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: -b[4])
    keep = []
    for b in boxes:
        if all(iou(b[:4], k[:4]) < thr for k in keep):
            keep.append(b)
    return keep


def run(name, fn, files, by_img, ph, di):
    tp = fn_ = fp = 0
    fp_cls = Counter()
    small_tp = small_total = 0
    for iid in ph[:N_PHONE_IMGS]:
        img = cv2.imread(os.path.join(IMG_DIR, files[iid]))
        if img is None:
            continue
        gts = [b for n, b in by_img[iid] if n == PHONE]
        preds = nms_boxes(fn(img))
        for g in gts:
            hit = any(iou(p[:4], g) >= 0.3 for p in preds)
            tp += hit
            fn_ += (not hit)
            if g[2] < 50:                       # small / distant phones
                small_total += 1
                small_tp += hit
    for iid in di[:N_DISTRACT_IMGS]:
        img = cv2.imread(os.path.join(IMG_DIR, files[iid]))
        if img is None:
            continue
        objs = by_img[iid]
        for p in nms_boxes(fn(img)):
            fp += 1
            bn, bi = "background", 0.0
            for n, b in objs:
                v = iou(p[:4], b)
                if v > bi:
                    bn, bi = n, v
            fp_cls[bn if bi >= 0.2 else "background"] += 1
    rec = tp / max(tp + fn_, 1)
    prec = tp / max(tp + fp, 1)
    small_rec = small_tp / max(small_total, 1)
    top = ", ".join(f"{k}:{v}" for k, v in fp_cls.most_common(4)) or "-"
    print(f"{name:<34} rec {rec*100:5.1f}%  small-rec {small_rec*100:5.1f}%  "
          f"FP {fp:3d}  prec {prec*100:5.1f}%  [{top}]", flush=True)
    return rec, fp


def main():
    files, by_img, ph, di = load()
    print(f"{min(len(ph), N_PHONE_IMGS)} phone images, "
          f"{N_DISTRACT_IMGS} phone-free images\n", flush=True)
    n = YOLO(os.path.join(BASE, "yolo11n.pt"))
    s = YOLO(os.path.join(BASE, "yolo11s.pt"))
    m = YOLO(os.path.join(BASE, "yolo11m.pt"))

    print("--- current server config (baseline) ---", flush=True)
    run("yolo11n@480 conf=0.60 [CURRENT]",
        lambda img: detect_full(n, img, 480, 0.60), files, by_img, ph, di)

    print("\n--- does lowering confidence actually break precision? ---", flush=True)
    for conf in [0.25, 0.40]:
        run(f"yolo11s@640 conf={conf}",
            lambda img, c=conf: detect_full(s, img, 640, c), files, by_img, ph, di)

    print("\n--- person-ROI: recovers small/distant/concealed phones? ---", flush=True)
    for conf in [0.25, 0.40]:
        run(f"yolo11s ROI conf={conf}",
            lambda img, c=conf: detect_roi(s, img, 640, c), files, by_img, ph, di)

    print("\n--- strongest candidate ---", flush=True)
    run("yolo11m ROI conf=0.40",
        lambda img: detect_roi(m, img, 640, 0.40), files, by_img, ph, di)


if __name__ == "__main__":
    main()
