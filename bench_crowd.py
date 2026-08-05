"""
bench_crowd.py — the "1 person in a crowd of 100" test, with real faces.

Enrols the live subject from a multi-angle burst, then builds a gallery
containing that subject PLUS 100 real identities from Labeled Faces in the
Wild. Measures whether the subject is still picked out correctly — including
when their probe image is degraded to simulate distance and low light.

This is the number that matters for a classroom: not "does it match me",
but "does it match me and NOT any of the other people present".
"""
import os
import sys
import time

import cv2
import numpy as np

import face_recog as fr

BASE = os.path.dirname(os.path.abspath(__file__))

FACE_WIDTHS = [160, 120, 80, 60, 45, 34, 28]
LIGHT_LEVELS = [1.0, 0.6, 0.4, 0.25]


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
    out += np.random.normal(0, 4.0 + 18.0 * (1.0 - level), out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def capture_burst(det, n=24):
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    if not cap.isOpened():
        print("camera did not open"); return []
    out, t0 = [], time.time()
    print(f"Capturing {n} frames — move your head naturally...")
    while len(out) < n and time.time() - t0 < 45:
        ok, f = cap.read()
        if not ok:
            time.sleep(0.1); continue
        hits = det.detect(f, thresh=0.5)
        if len(hits) == 1 and hits[0]["bbox"][2] >= 60:
            out.append((f.copy(), hits[0]))
            print(f"  {len(out)}/{n} (face {hits[0]['bbox'][2]:.0f}px)")
        time.sleep(0.22)
    cap.release()
    return out


def load_lfw_identities(det, emb, want=100):
    """One embedding per distinct LFW identity — a real impostor population."""
    try:
        from sklearn.datasets import fetch_lfw_people
    except ImportError:
        print("scikit-learn not available; cannot run the crowd test")
        return {}
    print(f"Loading LFW (downloads ~200MB once)...")
    people = fetch_lfw_people(min_faces_per_person=1, resize=1.0, color=True,
                              funneled=True, slice_=(slice(0, 250), slice(0, 250)))
    names = people.target_names
    imgs = people.images
    targets = people.target

    gallery = {}
    seen = set()
    for i in range(len(imgs)):
        t = int(targets[i])
        if t in seen:
            continue
        img = (imgs[i] * 255).astype(np.uint8)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        hits = det.detect(img, thresh=0.5)
        if not hits:
            continue
        v = emb.embed(img, hits[0]["kps"])
        if v is None:
            continue
        seen.add(t)
        gallery[f"LFW-{t}"] = (str(names[t]), v)
        if len(gallery) >= want:
            break
        if len(gallery) % 20 == 0:
            print(f"  {len(gallery)}/{want} impostor identities embedded")
    return gallery


def main():
    det = fr.SCRFDDetector()
    emb = fr.ArcFaceEmbedder()

    burst = capture_burst(det)
    if len(burst) < 8:
        print(f"only {len(burst)} usable frames; need >= 8")
        return
    half = len(burst) // 2
    enrol, test = burst[:half], burst[half:]

    subject_templates = []
    for img, f in enrol:
        v = emb.embed(img, f["kps"])
        if v is not None:
            subject_templates.append(v)
    print(f"\nsubject enrolled with {len(subject_templates)} templates")

    impostors = load_lfw_identities(det, emb, want=100)
    print(f"impostor population: {len(impostors)} real identities\n")

    gal = fr.Gallery()
    gal.set_person("SUBJECT", "Subject", subject_templates)
    for sid, (nm, v) in impostors.items():
        gal.set_person(sid, nm, [v])
    print(f"gallery size: {len(gal)} identities "
          f"(1 subject + {len(impostors)} others)\n")

    # ---- baseline: held-out clean probes -------------------------------
    correct = 0
    genuine_scores, runnerups = [], []
    for img, f in test:
        v = emb.embed(img, f["kps"])
        sid, nm, score, margin = gal.identify(v)
        sims = sorted(((float(np.max(p["templates"] @ v)), k)
                       for k, p in gal.people.items()), reverse=True)
        genuine = next(s for s, k in sims if k == "SUBJECT")
        best_imp = next(s for s, k in sims if k != "SUBJECT")
        genuine_scores.append(genuine); runnerups.append(best_imp)
        if sid == "SUBJECT":
            correct += 1
    print(f"=== CLEAN held-out probes ({len(test)}) ===")
    print(f"  correctly identified : {correct}/{len(test)}")
    print(f"  genuine score        : mean {np.mean(genuine_scores):.3f} "
          f"min {np.min(genuine_scores):.3f}")
    print(f"  best impostor score  : mean {np.mean(runnerups):.3f} "
          f"max {np.max(runnerups):.3f}")
    print(f"  separation margin    : {np.min(genuine_scores)-np.max(runnerups):+.3f}")

    probe_img, probe_face = test[0]
    native_w = probe_face["bbox"][2]

    # ---- distance ------------------------------------------------------
    print(f"\n=== IN A CROWD OF {len(impostors)}, vs DISTANCE ===")
    print(f"{'face px':>8} {'genuine':>8} {'best imp':>9} {'margin':>8}  {'result':<22}")
    for tw in FACE_WIDTHS:
        deg = simulate_distance(probe_img, native_w, tw)
        hits = det.detect(deg, thresh=0.4)
        if not hits:
            print(f"{tw:8d} {'-':>8} {'-':>9} {'-':>8}  {'face not detected':<22}")
            continue
        h = max(hits, key=lambda x: x["bbox"][2])
        v = emb.embed(deg, h["kps"])
        sims = sorted(((float(np.max(p["templates"] @ v)), k)
                       for k, p in gal.people.items()), reverse=True)
        g = next(s for s, k in sims if k == "SUBJECT")
        bi = next(s for s, k in sims if k != "SUBJECT")
        ok, reason, _ = fr.face_quality(deg, h["bbox"])
        sid, _n, _s, _m = gal.identify(v)
        if not ok:
            res = f"gated ({reason[:16]})"
        elif sid == "SUBJECT":
            res = "IDENTIFIED"
        elif sid is None:
            res = "no confident match"
        else:
            res = f"WRONG: {gal.people[sid]['name'][:14]}"
        print(f"{tw:8d} {g:8.3f} {bi:9.3f} {g-bi:+8.3f}  {res:<22}")

    # ---- low light -----------------------------------------------------
    print(f"\n=== IN A CROWD OF {len(impostors)}, vs LOW LIGHT ===")
    print(f"{'light':>8} {'genuine':>8} {'best imp':>9} {'margin':>8}  {'result':<22}")
    for lv in LIGHT_LEVELS:
        deg = simulate_lowlight(probe_img, lv)
        src = fr.enhance_lowlight(deg)
        hits = det.detect(src, thresh=0.4)
        if not hits:
            print(f"{lv:8.2f} {'-':>8} {'-':>9} {'-':>8}  {'face not detected':<22}")
            continue
        h = max(hits, key=lambda x: x["bbox"][2])
        v = emb.embed(src, h["kps"])
        sims = sorted(((float(np.max(p["templates"] @ v)), k)
                       for k, p in gal.people.items()), reverse=True)
        g = next(s for s, k in sims if k == "SUBJECT")
        bi = next(s for s, k in sims if k != "SUBJECT")
        sid, _n, _s, _m = gal.identify(v)
        res = ("IDENTIFIED" if sid == "SUBJECT"
               else "no confident match" if sid is None
               else f"WRONG: {gal.people[sid]['name'][:14]}")
        print(f"{lv:8.2f} {g:8.3f} {bi:9.3f} {g-bi:+8.3f}  {res:<22}")

    # ---- false accept rate across the whole impostor set ---------------
    print(f"\n=== FALSE ACCEPTS at threshold {fr.MATCH_THRESHOLD} ===")
    subj = np.asarray(subject_templates)
    imp_vs_subject = [float(np.max(subj @ v)) for _nm, v in impostors.values()]
    fa = sum(1 for s in imp_vs_subject if s >= fr.MATCH_THRESHOLD)
    print(f"  impostors scoring above threshold vs the subject: "
          f"{fa}/{len(imp_vs_subject)}")
    print(f"  highest impostor score: {max(imp_vs_subject):.3f}")
    print(f"  headroom before a false accept: "
          f"{fr.MATCH_THRESHOLD - max(imp_vs_subject):+.3f}")


if __name__ == "__main__":
    main()
