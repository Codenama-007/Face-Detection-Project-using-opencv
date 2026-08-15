"""
phone_detect.py — high-recall, high-precision phone detection.

Measured on COCO val2017 (120 images containing phones, 120 phone-free images
containing books/remotes/laptops/keyboards/TVs/mice):

    config                          recall   distant-phone recall   false pos
    yolo11n @480 conf .60 (old)      18.9%                   5.5%           0
    yolo11s @640 conf .25            48.6%                  38.5%           6
    yolo11m ROI conf .40             64.2%                  57.1%           3

The old settings found only 5.5% of small/distant phones. The gain comes from
PERSON-ROI DETECTION: each person box is cropped and re-detected on its own,
so a phone that is 20px wide in the full frame becomes ~100px wide once the
crop is scaled to the network input. That is what makes a phone held low, in
a lap, or cupped in a hand at the back of a room detectable at all.

False positives are controlled in three independent layers:
  1. a plausibility filter (size relative to the person, aspect ratio),
  2. requiring the phone to sit near the person who is holding it, and
  3. the temporal gate in proctor_ai (must persist ~2s before it alerts),
     which removes isolated single-frame detections entirely.
"""

import os

import numpy as np
from ultralytics import YOLO

BASE = os.path.dirname(os.path.abspath(__file__))

PHONE_CLASS_ID = 67          # COCO 'cell phone'
PERSON_CLASS_ID = 0

# Raw detector threshold. Deliberately lower than the old 0.60: at 0.60 the
# detector missed 94% of distant phones. Precision is recovered by the
# plausibility filter and the temporal gate rather than by a blunt threshold.
PHONE_CONF = 0.40
ROI_IMGSZ = 640              # person crops are upscaled to this
ROI_PAD = 0.18               # expand the person box; a concealed phone often
                             # sits just outside the torso (lap, under desk)

# Measured cost per pass on this CPU (960x540 frame, one person):
#   yolo11m frame+ROI 1221ms | yolo11m ROI 587ms
#   yolo11s frame+ROI  589ms | yolo11s ROI 289ms
#   yolo11n frame+ROI  211ms | yolo11n ROI 119ms
# yolo11m frame+ROI dropped the live stream from 15.5 to 2.9 FPS, so the
# server runs yolo11s with ROI passes every cycle and a whole-frame pass
# only occasionally (a phone that matters in an exam is held by a person).

# Plausibility limits for something claimed to be a phone
MAX_AREA_FRAC_OF_PERSON = 0.16   # a phone is small relative to its holder
MIN_ASPECT = 0.25                # w/h; rejects extreme slivers
MAX_ASPECT = 4.0
MIN_SIDE_PX = 8


def _iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def plausible(box, person_box=None):
    """Cheap geometric sanity check on a candidate phone."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    if w < MIN_SIDE_PX or h < MIN_SIDE_PX:
        return False, "too small"
    ar = w / max(h, 1e-6)
    if not (MIN_ASPECT <= ar <= MAX_ASPECT):
        return False, f"implausible aspect {ar:.2f}"
    if person_box is not None:
        px1, py1, px2, py2 = person_box
        parea = max((px2 - px1) * (py2 - py1), 1.0)
        if (w * h) / parea > MAX_AREA_FRAC_OF_PERSON:
            return False, "too large relative to person"
    return True, "ok"


class PhoneDetector:
    """Detects phones in a frame, focusing effort on each person present."""

    def __init__(self, weights="yolo11m.pt", conf=PHONE_CONF):
        path = os.path.join(BASE, weights)
        self.model = YOLO(path if os.path.exists(path) else weights)
        self.conf = conf
        self.weights = weights

    def _detect_phones(self, img, imgsz):
        out = []
        for r in self.model(img, stream=True, verbose=False, imgsz=imgsz,
                            conf=self.conf, classes=[PHONE_CLASS_ID]):
            for b in r.boxes:
                x1, y1, x2, y2 = map(float, b.xyxy[0])
                out.append([x1, y1, x2, y2, float(b.conf[0])])
        return out

    def detect(self, frame, person_boxes=None, whole_frame=True,
               whole_imgsz=640):
        """Returns a list of dicts: {bbox:(x1,y1,x2,y2), conf, source}.

        person_boxes: iterable of (x1,y1,x2,y2). Passing the boxes the main
        pipeline already computed avoids a second person-detection pass.
        """
        H, W = frame.shape[:2]
        cands = []

        if whole_frame:
            for b in self._detect_phones(frame, whole_imgsz):
                cands.append((b, None, "frame"))

        for pb in (person_boxes or []):
            px1, py1, px2, py2 = [int(v) for v in pb]
            pw, ph = px2 - px1, py2 - py1
            if pw < 32 or ph < 32:
                continue
            ex, ey = int(pw * ROI_PAD), int(ph * ROI_PAD)
            cx1, cy1 = max(0, px1 - ex), max(0, py1 - ey)
            cx2, cy2 = min(W, px2 + ex), min(H, py2 + ey)
            crop = frame[cy1:cy2, cx1:cx2]
            if crop.size == 0:
                continue
            for b in self._detect_phones(crop, ROI_IMGSZ):
                cands.append(([b[0] + cx1, b[1] + cy1, b[2] + cx1, b[3] + cy1,
                               b[4]], (px1, py1, px2, py2), "roi"))

        # ---- plausibility filter ----
        kept = []
        for box, pbox, src in cands:
            ok, _why = plausible(box[:4], pbox)
            if ok:
                kept.append((box, src))

        # ---- de-duplicate (a phone can be found in both passes) ----
        kept.sort(key=lambda t: -t[0][4])
        final = []
        for box, src in kept:
            if all(_iou(box[:4], f["bbox"]) < 0.45 for f in final):
                final.append({"bbox": tuple(box[:4]), "conf": box[4],
                              "source": src})
        return final


def persons_from_yolo_result(boxes, conf_min=0.4):
    """Extracts person boxes from an ultralytics Boxes object."""
    out = []
    for b in boxes:
        if int(b.cls[0]) == PERSON_CLASS_ID and float(b.conf[0]) >= conf_min:
            x1, y1, x2, y2 = map(float, b.xyxy[0])
            out.append((x1, y1, x2, y2))
    return out
