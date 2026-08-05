"""
bench_recognition.py — measures how far face recognition actually holds up.

Simulates distance by downscaling the detected face (a face 4x further away
occupies 1/4 the pixel width) and low light by scaling luminance and adding
sensor noise. Reports genuine vs impostor cosine scores so the decision
threshold can be chosen from data instead of guessed.

Run:  python bench_recognition.py            (captures live from webcam)
      python bench_recognition.py probe.jpg  (uses an image file)
"""
import os
import sys
import json

import cv2
import numpy as np
import psycopg2

BASE = os.path.dirname(os.path.abspath(__file__))
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_58LHqXDdanEy@ep-young-sea-aotvi360.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require")

detector = cv2.FaceDetectorYN.create(
    os.path.join(BASE, "models", "face_detection_yunet_2023mar.onnx"), "",
    (320, 320), 0.7, 0.3, 5000)
recognizer = cv2.FaceRecognizerSF.create(
    os.path.join(BASE, "models", "face_recognition_sface_2021dec.onnx"), "")

FACE_WIDTHS = [200, 160, 120, 100, 80, 60, 50, 40, 30, 24, 20]
LIGHT_LEVELS = [1.0, 0.6, 0.4, 0.25, 0.15, 0.08]


def detect_largest(img):
    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        return None
    return sorted(faces, key=lambda f: f[2] * f[3], reverse=True)[0]


def embed(img, face):
    aligned = recognizer.alignCrop(img, face)
    return recognizer.feature(aligned)


def simulate_distance(img, face, target_w):
    """Shrink the whole frame so the face is target_w px wide, then scale back
    up — reproducing the detail loss of a genuinely distant face."""
    fw = face[2]
    if fw <= 0:
        return None
    ratio = target_w / fw
    small = cv2.resize(img, (max(16, int(img.shape[1] * ratio)),
                             max(16, int(img.shape[0] * ratio))),
                       interpolation=cv2.INTER_AREA)
    # JPEG compression at the sensor, as a real camera would
    ok, enc = cv2.imencode('.jpg', small, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    small = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return cv2.resize(small, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_CUBIC)


def simulate_lowlight(img, level):
    """Scale luminance and add shot + read noise, as a real sensor does."""
    out = img.astype(np.float32) * level
    sigma = 4.0 + 18.0 * (1.0 - level)          # noise grows as light falls
    out += np.random.normal(0, sigma, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def load_enrolled():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("SELECT student_id, name, face_encoding FROM students;")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"sid": r[0], "name": r[1],
             "enc": np.array(r[2], dtype=np.float32)} for r in rows]


def main():
    if len(sys.argv) > 1:
        frame = cv2.imread(sys.argv[1])
        if frame is None:
            print(f"could not read {sys.argv[1]}"); return
    else:
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        best, best_area = None, 0
        for _ in range(30):                       # pick the sharpest capture
            ok, f = cap.read()
            if not ok:
                continue
            fa = detect_largest(f)
            if fa is not None and fa[2] * fa[3] > best_area:
                best, best_area = f.copy(), fa[2] * fa[3]
        cap.release()
        frame = best
        if frame is None:
            print("no face captured from webcam"); return
        cv2.imwrite(os.path.join(BASE, "bench_probe.jpg"), frame)

    face = detect_largest(frame)
    if face is None:
        print("no face detected in probe image"); return
    print(f"probe frame {frame.shape[1]}x{frame.shape[0]}, "
          f"native face width {face[2]:.0f}px\n")

    ref = embed(frame, face)
    enrolled = load_enrolled()

    # Which enrolled identity does this probe match best (the "genuine" one)?
    scored = sorted(
        ((recognizer.match(ref, e["enc"], cv2.FaceRecognizerSF_FR_COSINE), e)
         for e in enrolled), key=lambda t: -t[0])
    genuine = scored[0][1]
    print(f"best enrolled match: {genuine['name']} ({genuine['sid']}) "
          f"score {scored[0][0]:.3f}")

    # Impostors = every enrolled identity that is not this person
    impostors = [e for e in enrolled
                 if e["name"].lower().split()[0] != genuine["name"].lower().split()[0]]
    imp_scores = [recognizer.match(ref, e["enc"], cv2.FaceRecognizerSF_FR_COSINE)
                  for e in impostors]
    print(f"impostor scores over {len(impostors)} other identities: "
          f"max {max(imp_scores):.3f}, mean {np.mean(imp_scores):.3f}")
    print(f"current threshold 0.45 -> impostor margin "
          f"{0.45 - max(imp_scores):+.3f}\n")

    print("=== GENUINE score vs simulated DISTANCE (good light) ===")
    print(f"{'face px':>8} {'detected':>9} {'score':>7}  {'verdict':<12}")
    for tw in FACE_WIDTHS:
        deg = simulate_distance(frame, face, tw)
        f2 = detect_largest(deg)
        if f2 is None:
            print(f"{tw:8d} {'NO':>9} {'-':>7}  {'FACE LOST':<12}")
            continue
        s = recognizer.match(embed(deg, f2), genuine["enc"],
                             cv2.FaceRecognizerSF_FR_COSINE)
        verdict = "MATCH" if s >= 0.45 else "FAIL"
        print(f"{tw:8d} {'yes':>9} {s:7.3f}  {verdict:<12}")

    print("\n=== GENUINE score vs simulated LOW LIGHT (near distance) ===")
    print(f"{'light':>8} {'detected':>9} {'score':>7}  {'verdict':<12}")
    for lv in LIGHT_LEVELS:
        deg = simulate_lowlight(frame, lv)
        f2 = detect_largest(deg)
        if f2 is None:
            print(f"{lv:8.2f} {'NO':>9} {'-':>7}  {'FACE LOST':<12}")
            continue
        s = recognizer.match(embed(deg, f2), genuine["enc"],
                             cv2.FaceRecognizerSF_FR_COSINE)
        verdict = "MATCH" if s >= 0.45 else "FAIL"
        print(f"{lv:8.2f} {'yes':>9} {s:7.3f}  {verdict:<12}")

    print("\n=== COMBINED: distance x low light ===")
    print(f"{'face px':>8} " + " ".join(f"{lv:>7.2f}" for lv in LIGHT_LEVELS))
    for tw in [160, 100, 80, 60, 40]:
        row = [f"{tw:8d}"]
        for lv in LIGHT_LEVELS:
            deg = simulate_lowlight(simulate_distance(frame, face, tw), lv)
            f2 = detect_largest(deg)
            if f2 is None:
                row.append(f"{'lost':>7}")
            else:
                s = recognizer.match(embed(deg, f2), genuine["enc"],
                                     cv2.FaceRecognizerSF_FR_COSINE)
                row.append(f"{s:7.3f}")
        print(" ".join(row))


if __name__ == "__main__":
    main()
