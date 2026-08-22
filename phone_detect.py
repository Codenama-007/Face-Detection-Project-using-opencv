"""
phone_detect.py — High-recall, real-time, partial and small phone detection.

Optimized for CCTV and webcam invigilation:
- Detects full phones, 3/4, 1/2, 1/4 visible phones, angled phones,
  phones held in hands, lap level, desk level, and frame-edge partial phones.
- Combines high-resolution whole-frame scanning (640px) with dedicated
  person-ROI magnification (640px with generous hand/desk padding).
- Multi-layer verification: geometric sanity checks + NMS deduplication.
"""

import os
import numpy as np
from ultralytics import YOLO

BASE = os.path.dirname(os.path.abspath(__file__))

PHONE_CLASS_ID = 67          # COCO 'cell phone'
PERSON_CLASS_ID = 0

# Raw detector threshold configured for high recall on partial & occluded devices.
# Downstream temporal debouncing ensures zero phantom alerts.
PHONE_CONF = 0.25
ROI_IMGSZ = 640              # High-resolution magnification for person/hand crop
DEFAULT_WHOLE_IMGSZ = 640    # High-resolution whole-frame scanning

# Asymmetric person ROI padding:
# Expands generously downwards and sideways to enclose candidate hands, desk, and lap.
ROI_PAD_X = 0.35
ROI_PAD_Y_TOP = 0.20
ROI_PAD_Y_BOT = 0.45

# Plausibility limits for partial and full phones
MAX_AREA_FRAC_OF_PERSON = 0.28   # a phone relative to person box
MIN_ASPECT = 0.18                # w/h; allows vertical, horizontal, and partial slivers
MAX_ASPECT = 5.2
MIN_SIDE_PX = 6                  # minimum side length in pixels


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
    """Geometric plausibility check on a candidate phone bounding box."""
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
    """Detects full and partial mobile phones in real-time across frames."""

    def __init__(self, weights="yolo11s.pt", conf=PHONE_CONF):
        path = os.path.join(BASE, weights)
        self.model = YOLO(path if os.path.exists(path) else weights)
        self.conf = conf
        self.weights = weights

    def _detect_phones(self, img, imgsz):
        out = []
        try:
            for r in self.model(img, stream=True, verbose=False, imgsz=imgsz,
                                conf=self.conf, classes=[PHONE_CLASS_ID]):
                for b in r.boxes:
                    x1, y1, x2, y2 = map(float, b.xyxy[0])
                    out.append([x1, y1, x2, y2, float(b.conf[0])])
        except Exception as err:
            print(f"[PHONE_DETECT] Inference error: {err}")
        return out

    def detect(self, frame, person_boxes=None, whole_frame=True,
               whole_imgsz=DEFAULT_WHOLE_IMGSZ):
        """Returns a list of dicts: {bbox:(x1,y1,x2,y2), conf, device_type, source}.

        Processes whole frame and person-ROI crops with high resolution.
        """
        if frame is None or frame.size == 0:
            return []

        H, W = frame.shape[:2]
        cands = []

        # 1. High-resolution whole-frame scanning
        if whole_frame:
            for b in self._detect_phones(frame, whole_imgsz):
                cands.append((b, None, "frame"))

        # 2. Magnified Person-ROI scanning (hands, lap, desk region)
        for pb in (person_boxes or []):
            px1, py1, px2, py2 = [int(v) for v in pb]
            pw, ph = px2 - px1, py2 - py1
            if pw < 24 or ph < 24:
                continue

            ex = int(pw * ROI_PAD_X)
            ey_top = int(ph * ROI_PAD_Y_TOP)
            ey_bot = int(ph * ROI_PAD_Y_BOT)

            cx1, cy1 = max(0, px1 - ex), max(0, py1 - ey_top)
            cx2, cy2 = min(W, px2 + ex), min(H, py2 + ey_bot)
            crop = frame[cy1:cy2, cx1:cx2]
            if crop.size == 0:
                continue

            for b in self._detect_phones(crop, ROI_IMGSZ):
                cands.append(([b[0] + cx1, b[1] + cy1, b[2] + cx1, b[3] + cy1,
                               b[4]], (px1, py1, px2, py2), "roi"))

        # 3. Geometric plausibility validation
        kept = []
        for box, pbox, src in cands:
            ok, _why = plausible(box[:4], pbox)
            if ok:
                kept.append((box, src))

        # 4. NMS & IoU deduplication (merges whole-frame and ROI hits)
        kept.sort(key=lambda t: -t[0][4])
        final = []
        for box, src in kept:
            if all(_iou(box[:4], f["bbox"]) < 0.45 for f in final):
                w = box[2] - box[0]
                h = box[3] - box[1]
                area = w * h
                dev_type = "phone"
                if area < 1000 and 0.7 <= (w / max(h, 1)) <= 1.4:
                    dev_type = "smartwatch"
                elif area < 400:
                    dev_type = "earbud"

                final.append({
                    "bbox": (int(box[0]), int(box[1]), int(box[2]), int(box[3])),
                    "conf": float(box[4]),
                    "device_type": dev_type,
                    "source": src
                })

        return final


def persons_from_yolo_result(boxes, conf_min=0.35):
    """Extracts person boxes from an ultralytics Boxes object."""
    out = []
    for b in boxes:
        if int(b.cls[0]) == PERSON_CLASS_ID and float(b.conf[0]) >= conf_min:
            x1, y1, x2, y2 = map(float, b.xyxy[0])
            out.append((x1, y1, x2, y2))
    return out
