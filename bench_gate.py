"""bench_gate.py — verifies the quality gate accepts every face the model can
still recognise. Uses the saved probe image, so it needs no live camera."""
import os

import cv2
import numpy as np

import face_recog as fr

BASE = os.path.dirname(os.path.abspath(__file__))


def sim_dist(im, fw, tw):
    r = tw / max(fw, 1)
    s = cv2.resize(im, (max(16, int(im.shape[1] * r)), max(16, int(im.shape[0] * r))),
                   interpolation=cv2.INTER_AREA)
    ok, e = cv2.imencode('.jpg', s, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    s = cv2.imdecode(e, cv2.IMREAD_COLOR)
    return cv2.resize(s, (im.shape[1], im.shape[0]), interpolation=cv2.INTER_CUBIC)


def main():
    path = os.path.join(BASE, "bench_probe.jpg")
    img = cv2.imread(path)
    if img is None:
        print("bench_probe.jpg not found — run bench_recognition.py first")
        return

    det = fr.SCRFDDetector()
    emb = fr.ArcFaceEmbedder()
    hits = det.detect(img, thresh=0.5)
    if not hits:
        print("no face in probe image")
        return
    native = hits[0]["bbox"][2]
    ref = emb.embed(img, hits[0]["kps"])
    print(f"probe native face {native:.0f}px\n")
    print("Does the quality gate reject faces the model still recognises?")
    print(f"{'face px':>8} {'self-score':>11} {'sharpness':>10}  {'gate':<28}")

    for tw in [160, 120, 80, 60, 45, 34, 28, 24, 20, 16]:
        deg = sim_dist(img, native, tw)
        hh = det.detect(deg, thresh=0.4)
        if not hh:
            print(f"{tw:8d} {'-':>11} {'-':>10}  {'face not detected':<28}")
            continue
        h = max(hh, key=lambda x: x["bbox"][2])
        v = emb.embed(deg, h["kps"])
        score = float(np.dot(v, ref))
        ok, reason, m = fr.face_quality(deg, h["bbox"])
        verdict = "ACCEPTED" if ok else f"REJECTED ({reason})"
        flag = ""
        if not ok and score >= fr.MATCH_THRESHOLD:
            flag = "  <-- WRONGLY REJECTED"
        print(f"{tw:8d} {score:11.3f} {m.get('sharpness', 0):10.1f}  {verdict:<28}{flag}")


if __name__ == "__main__":
    main()
