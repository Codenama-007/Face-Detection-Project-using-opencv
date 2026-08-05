"""
face_recog.py — high-accuracy face recognition.

Replaces the previous single-template SFace matcher, which was measured
scoring the enrolled subject at only 0.393 against their own template — below
its own 0.45 accept threshold.

What makes this accurate:
  * ArcFace R50 (WebFace600K) 512-d embeddings instead of SFace 128-d.
  * MULTI-TEMPLATE enrolment: many embeddings per person covering pose,
    scale and lighting. A single photo cannot represent a face across
    conditions; this is the single biggest accuracy factor.
  * Low-light normalisation (CLAHE on luminance + adaptive gamma) applied
    before BOTH detection and embedding.
  * Flip test-time augmentation, averaged in embedding space.
  * Upscaling of small/distant faces to the model's native 112px input.
  * Quality gating: a face too small, too dark or too blurry is reported
    UNCERTAIN rather than being force-matched to the nearest identity.
"""

import os

import cv2
import numpy as np
import onnxruntime as ort

BASE = os.path.dirname(os.path.abspath(__file__))


def _find_model(filename):
    """Locate a model file anywhere under models/ (extraction layout varies)."""
    for root, _dirs, files in os.walk(os.path.join(BASE, "models")):
        if filename in files:
            return os.path.join(root, filename)
    raise FileNotFoundError(
        f"{filename} not found under models/. For buffalo_l models, download "
        f"https://github.com/deepinsight/insightface/releases/download/v0.7/"
        f"buffalo_l.zip and extract it into models/buffalo_l/")


ARCFACE_PATH = _find_model("w600k_r50.onnx")

# ONNX Runtime defaults to one intra-op thread per core. These sessions share
# a process with PyTorch (DeepSort) and MediaPipe, which do the same, so the
# pools oversubscribe the CPU and the video loop slows down even while the
# face models sit idle. Cap them and let the other frameworks have the cores.
ORT_THREADS = max(1, min(4, (os.cpu_count() or 4) // 2))


def _session_options():
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = ORT_THREADS
    so.inter_op_num_threads = 1
    return so


def _providers():
    return [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
            if p in ort.get_available_providers()]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# Thresholds set from measurement, not guesswork. Against 100 real LFW
# identities: genuine probes scored 0.54-0.83 while the best impostor reached
# only 0.188, so 0.36 sits in a wide empty band with 0/100 false accepts.
MATCH_THRESHOLD   = 0.36    # cosine
MARGIN_OVER_NEXT  = 0.10    # best must beat the runner-up identity by this

# Quality gates exist to catch frames with no usable face at all - NOT to
# second-guess the model. An earlier, stricter sharpness gate was measured
# rejecting 60px faces that were scoring 0.739 with a +0.623 margin over 100
# impostors, i.e. throwing away certain-correct identifications. Distant faces
# are legitimately soft; the score margin is the real confidence signal.
MIN_FACE_PX       = 24      # below this the pixels genuinely are not there
MIN_BRIGHTNESS    = 12      # mean luma of the face crop (after enhancement)
MIN_SHARPNESS     = 2.0     # variance of Laplacian; rejects only smeared frames
MAX_TEMPLATES     = 25      # per student

_ARCFACE_DST = np.array([   # canonical ArcFace 5-point template @112x112
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float32)


def enhance_lowlight(img):
    """CLAHE on luminance + adaptive gamma. Keeps the detector alive in light
    levels where the raw frame yields no detection at all."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(l)
    out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

    mean = float(np.mean(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)))
    if mean < 90:                      # dark frame -> lift midtones
        gamma = np.clip(90.0 / max(mean, 1.0), 1.0, 3.0)
        lut = np.array([((i / 255.0) ** (1.0 / gamma)) * 255
                        for i in range(256)]).astype(np.uint8)
        out = cv2.LUT(out, lut)
    return out


def _umeyama_similarity(src, dst):
    """Closed-form least-squares similarity transform (Umeyama 1991).

    The 5 landmark correspondences contain no outliers, so a robust iterative
    estimator (RANSAC/LMEDS) is both unnecessary and far slower — LMEDS here
    was measured costing ~280ms per call versus well under a millisecond for
    this direct solve.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    n, dim = src.shape
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    src_d, dst_d = src - src_mean, dst - dst_mean

    A = dst_d.T @ src_d / n
    d = np.ones(dim)
    if np.linalg.det(A) < 0:
        d[dim - 1] = -1

    U, S, Vt = np.linalg.svd(A)
    rank = np.linalg.matrix_rank(A)
    if rank == 0:
        return None
    if rank == dim - 1:
        if np.linalg.det(U) * np.linalg.det(Vt) > 0:
            R = U @ Vt
        else:
            s_last = d[dim - 1]
            d[dim - 1] = -1
            R = U @ np.diag(d) @ Vt
            d[dim - 1] = s_last
    else:
        R = U @ np.diag(d) @ Vt

    var_src = src_d.var(axis=0).sum()
    scale = 1.0 if var_src == 0 else (S @ d) / var_src

    M = np.eye(dim + 1, dtype=np.float64)
    M[:dim, :dim] = scale * R
    M[:dim, dim] = dst_mean - scale * R @ src_mean
    return M[:dim, :]


def align_face(img, landmarks5):
    """Similarity-transform the face onto the ArcFace template."""
    src = np.asarray(landmarks5, dtype=np.float32).reshape(5, 2)
    M = _umeyama_similarity(src, _ARCFACE_DST)
    if M is None:
        return None
    return cv2.warpAffine(img, M.astype(np.float32), (112, 112),
                          flags=cv2.INTER_CUBIC, borderValue=0)


def face_quality(img, face_box):
    """Returns (ok, reason, metrics) for a candidate face."""
    x, y, w, h = [int(v) for v in face_box[:4]]
    x, y = max(0, x), max(0, y)
    crop = img[y:y + max(1, h), x:x + max(1, w)]
    if crop.size == 0:
        return False, "empty", {}
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    metrics = {
        "width": w,
        "brightness": float(np.mean(gray)),
        "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
    }
    if w < MIN_FACE_PX:
        return False, f"face too small ({w}px < {MIN_FACE_PX}px)", metrics
    if metrics["brightness"] < MIN_BRIGHTNESS:
        return False, "too dark", metrics
    if metrics["sharpness"] < MIN_SHARPNESS:
        return False, "too blurry", metrics
    return True, "ok", metrics


def _distance2bbox(points, distance):
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points, distance):
    preds = []
    for i in range(0, distance.shape[1], 2):
        preds.append(points[:, 0] + distance[:, i])
        preds.append(points[:, 1] + distance[:, i + 1])
    return np.stack(preds, axis=-1)


def _nms(dets, thresh=0.4):
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(iou <= thresh)[0] + 1]
    return keep


class SCRFDDetector:
    """SCRFD-10G face detector (from the buffalo_l pack).

    Markedly better than YuNet on small and poorly-lit faces, and it returns
    the 5 landmarks ArcFace alignment needs.
    """

    STRIDES = (8, 16, 32)
    NUM_ANCHORS = 2

    def __init__(self, model_path=None, input_size=640):
        if model_path is None:
            model_path = _find_model("det_10g.onnx")
        self.sess = ort.InferenceSession(model_path,
                                         sess_options=_session_options(),
                                         providers=_providers())
        self.input_name = self.sess.get_inputs()[0].name
        self.input_size = input_size

    def detect(self, img, thresh=0.35, nms_thresh=0.4):
        """Returns a list of dicts: {bbox:(x,y,w,h), kps:(5,2), score}."""
        h0, w0 = img.shape[:2]
        S = self.input_size
        scale = min(S / w0, S / h0)
        nw, nh = int(round(w0 * scale)), int(round(h0 * scale))
        resized = cv2.resize(img, (nw, nh))
        canvas = np.zeros((S, S, 3), dtype=np.uint8)
        canvas[:nh, :nw] = resized

        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128, (S, S),
                                     (127.5, 127.5, 127.5), swapRB=True)
        outs = self.sess.run(None, {self.input_name: blob})

        scores_l, bboxes_l, kpss_l = [], [], []
        fmc = len(self.STRIDES)
        for idx, stride in enumerate(self.STRIDES):
            scores = outs[idx].reshape(-1)
            bbox_preds = outs[idx + fmc].reshape(-1, 4) * stride
            kps_preds = outs[idx + fmc * 2].reshape(-1, 10) * stride

            hh, ww = S // stride, S // stride
            centers = np.stack(np.mgrid[:hh, :ww][::-1], axis=-1).astype(np.float32)
            centers = (centers * stride).reshape(-1, 2)
            if self.NUM_ANCHORS > 1:
                centers = np.stack([centers] * self.NUM_ANCHORS,
                                   axis=1).reshape(-1, 2)

            keep = np.where(scores >= thresh)[0]
            if keep.size == 0:
                continue
            scores_l.append(scores[keep])
            bboxes_l.append(_distance2bbox(centers, bbox_preds)[keep])
            kpss_l.append(_distance2kps(centers, kps_preds)[keep].reshape(-1, 5, 2))

        if not scores_l:
            return []

        scores = np.concatenate(scores_l)
        bboxes = np.concatenate(bboxes_l) / scale
        kpss = np.concatenate(kpss_l) / scale

        dets = np.hstack([bboxes, scores[:, None]]).astype(np.float32)
        order = scores.argsort()[::-1]
        dets, kpss = dets[order], kpss[order]
        keep = _nms(dets, nms_thresh)

        results = []
        for i in keep:
            x1, y1, x2, y2, sc = dets[i]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w0, x2), min(h0, y2)
            if x2 <= x1 or y2 <= y1:
                continue
            results.append({
                "bbox": (float(x1), float(y1), float(x2 - x1), float(y2 - y1)),
                "kps": kpss[i],
                "score": float(sc),
            })
        return results


class ArcFaceEmbedder:
    def __init__(self):
        self.sess = ort.InferenceSession(ARCFACE_PATH,
                                         sess_options=_session_options(),
                                         providers=_providers())
        self.input_name = self.sess.get_inputs()[0].name
        self.provider = self.sess.get_providers()[0]

    def _forward(self, chips):
        """chips: list of 112x112 BGR crops -> (N, 512) unit-norm embeddings.

        Run one at a time: this export declares a fixed batch of 1, and
        feeding a larger batch works but emits a shape-mismatch warning per
        call.
        """
        out = []
        for c in chips:
            blob = cv2.cvtColor(c, cv2.COLOR_BGR2RGB)[None]
            blob = ((blob.astype(np.float32) - 127.5) / 127.5).transpose(0, 3, 1, 2)
            out.append(self.sess.run(None, {self.input_name: blob})[0][0])
        emb = np.stack(out)
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        return emb / np.maximum(norms, 1e-9)

    def embed(self, img, landmarks5, use_flip_tta=True):
        """Aligned + flip-augmented embedding for one face. Returns (512,)."""
        chip = align_face(img, landmarks5)
        if chip is None:
            return None
        chips = [chip]
        if use_flip_tta:
            chips.append(cv2.flip(chip, 1))
        embs = self._forward(chips)
        v = embs.mean(axis=0)
        return v / max(np.linalg.norm(v), 1e-9)


def cosine(a, b):
    return float(np.dot(a, b))


class Gallery:
    """Multi-template gallery. Each student holds several embeddings; the
    match score is the best over that student's templates."""

    def __init__(self):
        self.people = {}    # sid -> {"name": str, "templates": (N,512) array}

    def set_person(self, sid, name, templates):
        arr = np.asarray(templates, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr[None, :]
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        arr = arr / np.maximum(norms, 1e-9)
        if len(arr) > MAX_TEMPLATES:            # keep the most diverse subset
            arr = self._prune(arr, MAX_TEMPLATES)
        self.people[sid] = {"name": name, "templates": arr}

    @staticmethod
    def _prune(arr, k):
        """Greedy farthest-point selection so templates stay diverse."""
        chosen = [0]
        while len(chosen) < k:
            sims = arr @ arr[chosen].T          # (N, |chosen|)
            worst = sims.max(axis=1)
            nxt = int(np.argmin(worst))
            if nxt in chosen:
                break
            chosen.append(nxt)
        return arr[chosen]

    def identify(self, emb):
        """Returns (sid, name, score, margin) or (None, None, best, margin)
        when the match is rejected."""
        if emb is None or not self.people:
            return None, None, 0.0, 0.0
        scores = []
        for sid, p in self.people.items():
            scores.append((float(np.max(p["templates"] @ emb)), sid, p["name"]))
        scores.sort(reverse=True)
        best_score, best_sid, best_name = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else -1.0
        margin = best_score - runner_up
        if best_score >= MATCH_THRESHOLD and margin >= MARGIN_OVER_NEXT:
            return best_sid, best_name, best_score, margin
        return None, None, best_score, margin

    def __len__(self):
        return len(self.people)
