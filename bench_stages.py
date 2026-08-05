"""bench_stages.py — times each pipeline stage so tuning targets the real
bottleneck instead of a guess."""
import os
import time

import cv2
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
N = 15


def timeit(label, fn, n=N):
    fn()                       # warm up
    t0 = time.time()
    for _ in range(n):
        fn()
    ms = (time.time() - t0) / n * 1000
    print(f"  {label:<42} {ms:7.1f} ms")
    return ms


def main():
    img = cv2.imread(os.path.join(BASE, "bench_probe.jpg"))
    if img is None:
        img = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)
    frame = cv2.resize(img, (960, int(img.shape[0] * 960 / img.shape[1])))
    print(f"frame {frame.shape[1]}x{frame.shape[0]}, {N} iterations each\n")

    total = 0.0

    from ultralytics import YOLO
    yolo = YOLO(os.path.join(BASE, "yolo11n.pt"))
    total += timeit("YOLO11n (imgsz=480)",
                    lambda: list(yolo(frame, stream=True, verbose=False, imgsz=480)))

    import proctor_ai
    fa = proctor_ai.FaceAnalyzer(max_faces=6)
    total += timeit("MediaPipe FaceLandmarker (pose+gaze)",
                    lambda: fa.analyze(frame))

    from deep_sort_realtime.deepsort_tracker import DeepSort
    tracker = DeepSort(max_age=30)
    det = [([100, 100, 200, 400], 0.9, "person")]
    total += timeit("DeepSort update (1 person, torch embedder)",
                    lambda: tracker.update_tracks(det, frame=frame))

    total += timeit("JPEG encode (q70)",
                    lambda: cv2.imencode('.jpg', frame,
                                         [int(cv2.IMWRITE_JPEG_QUALITY), 70]))

    print(f"  {'-'*42} {'-'*7}")
    print(f"  {'PER-FRAME SUBTOTAL':<42} {total:7.1f} ms"
          f"  -> {1000/max(total,1):.1f} FPS ceiling")

    print("\n  Identification pass (periodic, NOT every frame):")
    import face_recog as fr
    scrfd = fr.SCRFDDetector()
    emb = fr.ArcFaceEmbedder()
    t_det = timeit("SCRFD detect", lambda: scrfd.detect(frame, thresh=0.45))
    hits = scrfd.detect(frame, thresh=0.4)
    if hits:
        kps = hits[0]["kps"]
        t_emb = timeit("ArcFace embed (with flip TTA)",
                       lambda: emb.embed(frame, kps))
    else:
        t_emb = 0.0
        print("    (no face in probe; skipped embed timing)")
    t_enh = timeit("CLAHE low-light enhance", lambda: fr.enhance_lowlight(frame))
    print(f"  {'-'*42} {'-'*7}")
    print(f"  {'ID PASS TOTAL':<42} {t_det+t_emb+t_enh:7.1f} ms")
    print(f"\n  At ID_INTERVAL_FAST=0.4s that adds "
          f"{(t_det+t_emb+t_enh)/400*100:.0f}% average load;")
    print(f"  at ID_INTERVAL_SLOW=3.0s it adds "
          f"{(t_det+t_emb+t_enh)/3000*100:.0f}%.")


if __name__ == "__main__":
    main()
