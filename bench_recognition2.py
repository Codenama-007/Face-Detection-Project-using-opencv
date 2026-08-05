"""
bench_recognition2.py — A/B: old SFace single-template vs new ArcFace
multi-template, on identical degraded inputs.

Captures a short live burst, enrols from the first half, and tests on the
held-out second half degraded to simulate distance and low light.
"""
import os
import sys
import time

import cv2
import numpy as np

import face_recog as fr

BASE = os.path.dirname(os.path.abspath(__file__))

detector = cv2.FaceDetectorYN.create(
    os.path.join(BASE, "models", "face_detection_yunet_2023mar.onnx"), "",
    (320, 320), 0.6, 0.3, 5000)
sface = cv2.FaceRecognizerSF.create(
    os.path.join(BASE, "models", "face_recognition_sface_2021dec.onnx"), "")

FACE_WIDTHS = [200, 160, 120, 100, 80, 60, 50, 40, 34, 28]
LIGHT_LEVELS = [1.0, 0.6, 0.4, 0.25, 0.15, 0.08]


def detect(img, enhance=False):
    work = fr.enhance_lowlight(img) if enhance else img
    h, w = work.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(work)
    if faces is None or len(faces) == 0:
        return None, work
    return sorted(faces, key=lambda f: f[2] * f[3], reverse=True)[0], work


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


def capture_burst(n=24):
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    if not cap.isOpened():
        print("ERROR: camera did not open (is the server holding it?)")
        cap.release()
        return []
    frames = []
    read_fail = no_face = 0
    print(f"Capturing {n} frames — move your head naturally (look left, "
          f"right, up, down, closer, further)...")
    t0 = time.time()
    while len(frames) < n and time.time() - t0 < 45:
        ok, f = cap.read()
        if not ok:
            read_fail += 1
            time.sleep(0.1)          # don't spin the timeout away
            continue
        fa, _ = detect(f)
        if fa is not None and fa[2] >= 60:
            frames.append((f.copy(), fa))
            print(f"  captured {len(frames)}/{n} (face {fa[2]:.0f}px)")
        else:
            no_face += 1
        time.sleep(0.25)
    cap.release()
    print(f"  [read failures: {read_fail}, frames without a usable face: {no_face}]")
    return frames


def main():
    burst = capture_burst()
    if len(burst) < 8:
        print(f"only captured {len(burst)} usable frames; need >= 8")
        return

    half = len(burst) // 2
    enrol, test = burst[:half], burst[half:]
    print(f"enrolling from {len(enrol)} frames, testing on {len(test)} held-out\n")

    embedder = fr.ArcFaceEmbedder()

    # --- build galleries -------------------------------------------------
    arc_templates = []
    for img, fa in enrol:
        v = embedder.embed(img, fa[4:14])
        if v is not None:
            arc_templates.append(v)
    gallery = fr.Gallery()
    gallery.set_person("SELF", "Subject", arc_templates)
    print(f"ArcFace gallery: {len(gallery.people['SELF']['templates'])} templates")

    # old system: ONE SFace template (as the current DB stores)
    img0, fa0 = enrol[0]
    sface_ref = sface.feature(sface.alignCrop(img0, fa0))

    def arc_score(img, fa):
        v = embedder.embed(img, fa[4:14])
        if v is None:
            return 0.0
        return float(np.max(gallery.people["SELF"]["templates"] @ v))

    def sface_score(img, fa):
        return sface.match(sface.feature(sface.alignCrop(img, fa)), sface_ref,
                           cv2.FaceRecognizerSF_FR_COSINE)

    # --- clean held-out ---------------------------------------------------
    a_clean = [arc_score(i, f) for i, f in test]
    s_clean = [sface_score(i, f) for i, f in test]
    print(f"\nHeld-out clean frames:")
    print(f"  SFace  (1 template) : mean {np.mean(s_clean):.3f}  min {np.min(s_clean):.3f}")
    print(f"  ArcFace(multi-tmpl) : mean {np.mean(a_clean):.3f}  min {np.min(a_clean):.3f}")

    probe_img, probe_face = test[0]
    native_w = probe_face[2]

    # --- distance ---------------------------------------------------------
    print(f"\n=== DISTANCE (native face {native_w:.0f}px, good light) ===")
    print(f"{'face px':>8} {'SFace':>8} {'ArcFace':>8}  {'ArcFace verdict':<16}")
    for tw in FACE_WIDTHS:
        deg = simulate_distance(probe_img, native_w, tw)
        fa, work = detect(deg, enhance=True)
        if fa is None:
            print(f"{tw:8d} {'lost':>8} {'lost':>8}  {'FACE NOT FOUND':<16}")
            continue
        a = arc_score(work, fa)
        s = sface_score(work, fa)
        ok, reason, _ = fr.face_quality(work, fa)
        verdict = ("MATCH" if a >= fr.MATCH_THRESHOLD else "reject") if ok \
                  else f"gated: {reason[:14]}"
        print(f"{tw:8d} {s:8.3f} {a:8.3f}  {verdict:<16}")

    # --- low light --------------------------------------------------------
    print(f"\n=== LOW LIGHT (near distance) ===")
    print(f"{'light':>8} {'SFace':>8} {'ArcFace':>8}  {'detector':<22}")
    for lv in LIGHT_LEVELS:
        deg = simulate_lowlight(probe_img, lv)
        fa_raw, _ = detect(deg, enhance=False)
        fa, work = detect(deg, enhance=True)
        det = ("raw+enhanced" if fa_raw is not None and fa is not None else
               "ENHANCED ONLY" if fa is not None else "lost")
        if fa is None:
            print(f"{lv:8.2f} {'lost':>8} {'lost':>8}  {det:<22}")
            continue
        a = arc_score(work, fa)
        s = sface_score(work, fa)
        print(f"{lv:8.2f} {s:8.3f} {a:8.3f}  {det:<22}")

    # --- combined ---------------------------------------------------------
    print(f"\n=== COMBINED distance x low light (ArcFace) ===")
    print(f"{'face px':>8} " + " ".join(f"{lv:>7.2f}" for lv in LIGHT_LEVELS))
    for tw in [160, 100, 80, 60, 40]:
        row = [f"{tw:8d}"]
        for lv in LIGHT_LEVELS:
            deg = simulate_lowlight(simulate_distance(probe_img, native_w, tw), lv)
            fa, work = detect(deg, enhance=True)
            row.append(f"{'lost':>7}" if fa is None else f"{arc_score(work, fa):7.3f}")
        print(" ".join(row))

    print(f"\nArcFace accept threshold = {fr.MATCH_THRESHOLD}")


if __name__ == "__main__":
    main()
