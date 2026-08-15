"""bench_phone4.py — ROI-only comparison of the models the server can run,
so the speed/accuracy dial is chosen from data.

Matches what the server actually does: person boxes from yolo11n@480, then a
phone pass inside each person crop.
"""
import json
import os
from collections import Counter, defaultdict

import cv2
from ultralytics import YOLO

import phone_detect as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "datasets")
IMG_DIR = os.path.join(DATA, "val2017")
ANN = os.path.join(DATA, "annotations", "instances_val2017.json")
PHONE = "cell phone"
DISTRACTORS = {"book", "remote", "laptop", "keyboard", "tv", "mouse"}
N_PHONE = int(os.environ.get("N_PHONE", 120))
N_DISTRACT = int(os.environ.get("N_DISTRACT", 120))


def iou_xywh(p, g):
    px1, py1, px2, py2 = p
    gx1, gy1, gw, gh = g
    gx2, gy2 = gx1 + gw, gy1 + gh
    ix1, iy1 = max(px1, gx1), max(py1, gy1)
    ix2, iy2 = min(px2, gx2), min(py2, gy2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    u = (px2 - px1) * (py2 - py1) + gw * gh - inter
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


def evaluate(label, fn, files, by_img, ph, di):
    tp = miss = fp = 0
    stp = sn = 0
    fpc = Counter()
    for iid in ph[:N_PHONE]:
        img = cv2.imread(os.path.join(IMG_DIR, files[iid]))
        if img is None:
            continue
        preds = fn(img)
        for n, g in by_img[iid]:
            if n != PHONE:
                continue
            hit = any(iou_xywh(p, g) >= 0.3 for p in preds)
            tp += hit; miss += (not hit)
            if g[2] < 50:
                sn += 1; stp += hit
    for iid in di[:N_DISTRACT]:
        img = cv2.imread(os.path.join(IMG_DIR, files[iid]))
        if img is None:
            continue
        for p in fn(img):
            fp += 1
            bn, bi = "background", 0.0
            for n, b in by_img[iid]:
                v = iou_xywh(p, b)
                if v > bi:
                    bn, bi = n, v
            fpc[bn if bi >= 0.2 else "background"] += 1
    rec = tp / max(tp + miss, 1)
    prec = tp / max(tp + fp, 1)
    srec = stp / max(sn, 1)
    top = ", ".join(f"{k}:{v}" for k, v in fpc.most_common(4)) or "-"
    print(f"{label:<34} rec {rec*100:5.1f}%  distant {srec*100:5.1f}%  "
          f"FP {fp:3d}  prec {prec*100:5.1f}%  [{top}]", flush=True)


def main():
    files, by_img, ph, di = load()
    print(f"{min(len(ph),N_PHONE)} phone images, {N_DISTRACT} phone-free "
          f"(ROI-only, matching the server)\n", flush=True)

    pmodel = YOLO(os.path.join(BASE, "yolo11n.pt"))

    def persons(img):
        out = []
        for r in pmodel(img, stream=True, verbose=False, imgsz=480, conf=0.4,
                        classes=[0]):
            out.extend(pd.persons_from_yolo_result(r.boxes))
        return out

    for w in ["yolo11n.pt", "yolo11s.pt", "yolo11m.pt"]:
        det = pd.PhoneDetector(w)
        evaluate(f"{w} ROI-only",
                 lambda img, d=det: [x["bbox"] for x in
                                     d.detect(img, persons(img), whole_frame=False)],
                 files, by_img, ph, di)


if __name__ == "__main__":
    main()
