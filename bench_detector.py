"""bench_detector.py — YuNet vs SCRFD on low light and distance.

The recognition benchmark showed detection, not recognition, is the limit in
low light: below ~0.25 illumination YuNet returns nothing, so the recogniser
never sees a face. This measures whether SCRFD + enhancement extends that.
"""
import os
import time

import cv2
import numpy as np

import face_recog as fr

BASE = os.path.dirname(os.path.abspath(__file__))
yunet = cv2.FaceDetectorYN.create(
    os.path.join(BASE, "models", "face_detection_yunet_2023mar.onnx"), "",
    (320, 320), 0.6, 0.3, 5000)

LIGHT_LEVELS = [1.0, 0.6, 0.4, 0.25, 0.15, 0.10, 0.06, 0.04]
FACE_WIDTHS = [160, 120, 80, 60, 45, 34, 26, 20, 16]


def yunet_detect(img):
    h, w = img.shape[:2]
    yunet.setInputSize((w, h))
    _, faces = yunet.detect(img)
    if faces is None or len(faces) == 0:
        return None
    return sorted(faces, key=lambda f: f[2] * f[3], reverse=True)[0]


def simulate_distance(img, face_w, target_w):
    ratio = target_w / max(face_w, 1)
    small = cv2.resize(img, (max(16, int(img.shape[1] * ratio)),
                             max(16, int(img.shape[0] * ratio))),
                       interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode('.jpg', small, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    small = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return cv2.resize(small, (img.shape[1], img.shape[0]),
                      interpolation=cv2.INTER_CUBIC)


def simulate_lowlight(img, level):
    out = img.astype(np.float32) * level
    sigma = 4.0 + 18.0 * (1.0 - level)
    out += np.random.normal(0, sigma, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def main():
    probe_path = os.path.join(BASE, "bench_probe.jpg")
    if os.path.exists(probe_path):
        frame = cv2.imread(probe_path)
    else:
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        frame = None
        for _ in range(20):
            ok, f = cap.read()
            if ok and yunet_detect(f) is not None:
                frame = f
                break
        cap.release()
    if frame is None:
        print("no probe frame available")
        return

    scrfd = fr.SCRFDDetector()
    base_face = yunet_detect(frame)
    native_w = base_face[2] if base_face is not None else 150
    print(f"probe {frame.shape[1]}x{frame.shape[0]}, native face {native_w:.0f}px\n")

    print("=== LOW LIGHT: can each detector find the face at all? ===")
    print(f"{'light':>7} {'YuNet':>10} {'YuNet+enh':>11} {'SCRFD':>10} {'SCRFD+enh':>11}")
    for lv in LIGHT_LEVELS:
        deg = simulate_lowlight(frame, lv)
        enh = fr.enhance_lowlight(deg)
        y_raw = "yes" if yunet_detect(deg) is not None else "-"
        y_enh = "yes" if yunet_detect(enh) is not None else "-"
        s_raw = scrfd.detect(deg)
        s_enh = scrfd.detect(enh)
        sr = f"{s_raw[0]['score']:.2f}" if s_raw else "-"
        se = f"{s_enh[0]['score']:.2f}" if s_enh else "-"
        print(f"{lv:7.2f} {y_raw:>10} {y_enh:>11} {sr:>10} {se:>11}")

    print("\n=== DISTANCE: smallest detectable face (good light) ===")
    print(f"{'face px':>8} {'YuNet':>10} {'SCRFD':>10}")
    for tw in FACE_WIDTHS:
        deg = simulate_distance(frame, native_w, tw)
        y = "yes" if yunet_detect(deg) is not None else "-"
        hits = scrfd.detect(deg)
        s = f"{hits[0]['score']:.2f}" if hits else "-"
        print(f"{tw:8d} {y:>10} {s:>10}")

    # speed
    t0 = time.time()
    for _ in range(10):
        scrfd.detect(frame)
    print(f"\nSCRFD speed: {(time.time()-t0)/10*1000:.0f} ms/frame at "
          f"{frame.shape[1]}x{frame.shape[0]}")


if __name__ == "__main__":
    main()
