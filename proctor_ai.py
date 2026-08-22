"""
proctor_ai.py — Core AI pipeline for the ProctorAI exam monitoring system.

Pipeline (per processed frame):
    Frame -> FaceMesh landmarks -> Head pose (solvePnP, degrees)
          -> Iris gaze estimation -> Mouth activity (talking)
          -> TemporalBehaviorEngine -> Suspicion score -> Dashboard state

Design principles:
  * NO alert is ever generated from a single frame. Every event must be
    sustained for a minimum duration, or repeat a minimum number of times,
    before it is confirmed.
  * Per-student baseline calibration: head-pose thresholds are relative to
    each student's natural resting pose, not absolute zero.
  * Natural behaviour (blinks, brief glances, posture shifts) is explicitly
    ignored: glances shorter than GLANCE_IGNORE_S never contribute.
  * Suspicion is a decaying score with Low/Medium/High/Critical tiers,
    never a binary "cheating" flag.
"""

import math
import os
import time
from collections import deque

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks.python import vision as mp_vision
from mediapipe.tasks.python.core.base_options import BaseOptions

_LANDMARKER_MODEL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "face_landmarker.task")

if not os.path.exists(_LANDMARKER_MODEL):
    try:
        import urllib.request
        os.makedirs(os.path.dirname(_LANDMARKER_MODEL), exist_ok=True)
        print("[AI] Downloading MediaPipe face_landmarker.task...")
        urllib.request.urlretrieve(
            "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
            _LANDMARKER_MODEL
        )
        print("[AI] MediaPipe face_landmarker.task downloaded successfully.")
    except Exception as e:
        print(f"[AI] Error downloading face_landmarker.task: {e}")

# ---------------------------------------------------------------------------
# Tunables (all thresholds in one place)
# ---------------------------------------------------------------------------

# If left/right labels appear mirrored for your camera, flip this.
LABEL_FLIP = False

# Head pose direction thresholds (degrees, relative to calibrated baseline)
YAW_AWAY_DEG      = 25.0     # sustained side look
YAW_ASSIST_DEG    = 12.0     # moderate head turn counts if gaze agrees
YAW_BEHIND_DEG    = 60.0     # looking behind
PITCH_DOWN_DEG    = 20.0     # looking down (desk/lap)
PITCH_UP_DEG      = 17.0     # looking up
RAPID_YAW_DEG_S   = 120.0    # deg/sec spike = rapid head movement

# Gaze (iris position within the eye, 0..1; 0.5 = centered)
GAZE_SIDE_LO      = 0.40
GAZE_SIDE_HI      = 0.60
GAZE_V_UP         = 0.33
GAZE_V_DOWN       = 0.67

# Mouth (jawOpen blendshape score, 0..1)
JAW_OPEN_SCORE    = 0.22
TALK_TRANSITIONS  = 6        # open<->close flips within TALK_WINDOW_S
TALK_WINDOW_S     = 3.0

# Temporal behaviour
GLANCE_IGNORE_S   = 0.30     # anything shorter is natural, never counted
BASELINE_FRAMES   = 15       # fast baseline calibration (<1s) for immediate engagement
GLANCE_MIN_S      = 0.30     # a "glance" for repetition counting
GLANCE_MAX_S      = 3.0
REPEAT_GLANCES_N  = 8        # glances per REPEAT_WINDOW_S to alert
REPEAT_WINDOW_S   = 60.0
RESET_CLEAR_S     = 1.5      # condition must be false this long to re-arm
ESCALATE_EVERY_S  = 45.0     # a behaviour that just persists re-scores slowly
ACQUIRE_SETTLE_S  = 1.0      # settle period after acquiring a face

# Suspicion score
SCORE_DECAY_PER_S = 1.0      # points/sec of clean behaviour
SCORE_MAX         = 100.0    # tiers are defined on a 0-100 scale
TIERS = [(20, "LOW"), (50, "MEDIUM"), (80, "HIGH"), (float("inf"), "CRITICAL")]

# Minimum model confidences (detections below these are ignored entirely)
CONF_FACE   = 0.60
CONF_PHONE  = 0.25   # Responsive and accurate phone confidence threshold for full & partial phones

# Event definitions: points, min sustained duration (s), cooldown (s)
EVENTS = {
    "PHONE_VISIBLE":    {"points": 100, "min_s": 0.20, "cooldown": 6,  "label": "Phone visible"},
    "EXTRA_PERSON":     {"points": 80,  "min_s": 2.5, "cooldown": 20, "label": "Another person in frame"},
    "CAMERA_BLOCKED":   {"points": 60,  "min_s": 1.5, "cooldown": 15, "label": "Camera blocked"},
    "LOOKING_BEHIND":   {"points": 45,  "min_s": 1.5, "cooldown": 10, "label": "Looking behind"},
    "FACE_MISSING":     {"points": 40,  "min_s": 2.5, "cooldown": 12, "label": "Face missing"},
    "FACE_COVERED":     {"points": 35,  "min_s": 2.5, "cooldown": 12, "label": "Face covered / occluded"},
    "LOOKING_AWAY":     {"points": 20,  "min_s": 4.0, "cooldown": 8,  "label": "Sustained side look"},
    "LOOKING_DOWN":     {"points": 15,  "min_s": 4.0, "cooldown": 10, "label": "Looking down repeatedly"},
    "LOOKING_UP":       {"points": 10,  "min_s": 4.0, "cooldown": 10, "label": "Looking up repeatedly"},
    "TALKING":          {"points": 10,  "min_s": 3.0, "cooldown": 12, "label": "Talking detected"},
    "RAPID_MOVEMENT":   {"points": 15,  "min_s": 0.0, "cooldown": 15, "label": "Rapid head movement"},
    "REPEATED_GLANCES": {"points": 25,  "min_s": 0.0, "cooldown": 30, "label": "Repeated side glances"},
}


def tier_for(score):
    for limit, name in TIERS:
        if score < limit:
            return name
    return "CRITICAL"


# ---------------------------------------------------------------------------
# Face analysis (FaceMesh -> head pose + gaze + mouth)
# ---------------------------------------------------------------------------

class FaceObservation:
    """One face found in one frame, with derived signals and eye/iris landmarks."""
    __slots__ = ("nose_xy", "bbox", "yaw", "pitch", "roll", "gaze_h", "gaze_v",
                 "mouth_open", "confidence", "left_iris_xy", "right_iris_xy",
                 "left_eye_center", "right_eye_center", "raw_gaze")

    def __init__(self):
        self.nose_xy = (0, 0)
        self.bbox = (0, 0, 0, 0)
        self.yaw = 0.0
        self.pitch = 0.0
        self.roll = 0.0
        self.gaze_h = 0.5
        self.gaze_v = 0.5
        self.mouth_open = False
        self.confidence = 0.0
        self.left_iris_xy = None
        self.right_iris_xy = None
        self.left_eye_center = None
        self.right_eye_center = None
        self.raw_gaze = "CENTER"


class FaceAnalyzer:
    """Runs the MediaPipe FaceLandmarker (Tasks API, 478 landmarks incl. iris)
    once per frame and derives head pose, gaze and mouth activity for every
    visible face."""

    def __init__(self, max_faces=6):
        options = mp_vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=_LANDMARKER_MODEL),
            running_mode=mp_vision.RunningMode.VIDEO,
            num_faces=max_faces,
            min_face_detection_confidence=CONF_FACE,
            min_tracking_confidence=0.5,
            # The model's own head-pose matrix and gaze/mouth blendshapes are
            # far more stable than deriving them by hand from raw landmarks.
            output_facial_transformation_matrixes=True,
            output_face_blendshapes=True,
        )
        self._lm = mp_vision.FaceLandmarker.create_from_options(options)
        self._t0 = time.monotonic()
        self._last_ts = -1

    def close(self):
        self._lm.close()

    def analyze(self, frame_bgr):
        """Returns a list of FaceObservation for the frame."""
        h, w = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        # VIDEO mode requires strictly increasing timestamps
        ts = int((time.monotonic() - self._t0) * 1000)
        if ts <= self._last_ts:
            ts = self._last_ts + 1
        self._last_ts = ts
        result = self._lm.detect_for_video(mp_img, ts)

        out = []
        if not result.face_landmarks:
            return out

        mats = result.facial_transformation_matrixes or []
        shapes = result.face_blendshapes or []

        for i, lms in enumerate(result.face_landmarks):
            pts = np.array([(lm.x * w, lm.y * h) for lm in lms],
                           dtype=np.float64)
            obs = FaceObservation()
            obs.confidence = 1.0  # model gates internally on CONF_FACE
            obs.nose_xy = tuple(pts[1].astype(int))
            xs, ys = pts[:, 0], pts[:, 1]
            obs.bbox = (int(xs.min()), int(ys.min()),
                        int(xs.max() - xs.min()), int(ys.max() - ys.min()))

            # Extract real iris and eye landmarks (MediaPipe 478-landmark topology)
            if len(pts) >= 478:
                obs.left_iris_xy = (int(pts[468, 0]), int(pts[468, 1]))
                obs.right_iris_xy = (int(pts[473, 0]), int(pts[473, 1]))
            if len(pts) >= 363:
                obs.left_eye_center = (int((pts[33, 0] + pts[133, 0]) / 2), int((pts[33, 1] + pts[133, 1]) / 2))
                obs.right_eye_center = (int((pts[362, 0] + pts[263, 0]) / 2), int((pts[362, 1] + pts[263, 1]) / 2))

            # ---- Head pose from the model's own 4x4 transformation matrix.
            if i < len(mats):
                R = np.array(mats[i])[:3, :3]
                sy = math.sqrt(R[0, 0] ** 2 + R[1, 0] ** 2)
                if sy > 1e-6:
                    pitch = math.degrees(math.atan2(R[2, 1], R[2, 2]))
                    yaw = math.degrees(math.atan2(-R[2, 0], sy))
                    roll = math.degrees(math.atan2(R[1, 0], R[0, 0]))
                else:
                    pitch = math.degrees(math.atan2(-R[1, 2], R[1, 1]))
                    yaw = math.degrees(math.atan2(-R[2, 0], sy))
                    roll = 0.0
                obs.yaw, obs.pitch, obs.roll = yaw, -pitch, roll

            # ---- Gaze + mouth from trained blendshapes ----
            if i < len(shapes):
                b = {c.category_name: c.score for c in shapes[i]}

                look_left = max(b.get("eyeLookOutLeft", 0.0),
                                b.get("eyeLookInRight", 0.0))
                look_right = max(b.get("eyeLookOutRight", 0.0),
                                 b.get("eyeLookInLeft", 0.0))
                obs.gaze_h = float(np.clip(0.5 + (look_right - look_left) / 2, 0, 1))

                look_up = (b.get("eyeLookUpLeft", 0.0) + b.get("eyeLookUpRight", 0.0)) / 2
                look_down = (b.get("eyeLookDownLeft", 0.0) + b.get("eyeLookDownRight", 0.0)) / 2
                obs.gaze_v = float(np.clip(0.5 + (look_down - look_up) / 2, 0, 1))

                blink = max(b.get("eyeBlinkLeft", 0.0), b.get("eyeBlinkRight", 0.0))
                if blink > 0.5:
                    obs.gaze_v = 0.5

                obs.mouth_open = bool(b.get("jawOpen", 0.0) > JAW_OPEN_SCORE)

            # Compute raw instant gaze label
            raw_gaze = "CENTER"
            if obs.gaze_h < 0.38:
                raw_gaze = "RIGHT" if LABEL_FLIP else "LEFT"
            elif obs.gaze_h > 0.62:
                raw_gaze = "LEFT" if LABEL_FLIP else "RIGHT"
            elif obs.gaze_v < 0.35:
                raw_gaze = "UP"
            elif obs.gaze_v > 0.65:
                raw_gaze = "DOWN"
            obs.raw_gaze = raw_gaze

            out.append(obs)
        return out


# ---------------------------------------------------------------------------
# Temporal behaviour engine + suspicion scoring (per student)
# ---------------------------------------------------------------------------

class _EventState:
    __slots__ = ("active_since", "last_alert", "fired", "clear_since")

    def __init__(self):
        self.active_since = None   # when the condition became true
        self.last_alert = 0.0      # wall time of the last alert
        self.fired = False         # already alerted for THIS occurrence
        self.clear_since = None    # when the condition became false


class StudentBehavior:
    """Tracks one student's behaviour over time and maintains their
    suspicion score. All alerts are temporal — never single-frame."""

    def __init__(self, sid, name):
        self.sid = sid
        self.name = name
        self.score = 0.0
        self.alerts = deque(maxlen=60)          # confirmed alert history
        self.events = {k: _EventState() for k in EVENTS}
        self._baseline_yaw = deque(maxlen=BASELINE_FRAMES)
        self._baseline_pitch = deque(maxlen=BASELINE_FRAMES)
        self._yaw_ema = None
        self._pitch_ema = None
        self._gh_ema = None
        self._gv_ema = None
        self._gv_baseline = deque(maxlen=BASELINE_FRAMES)
        self._gaze_votes = deque(maxlen=5)
        self._prev_yaw = None
        self._prev_t = None
        self._settle_until = 0.0                # ignore pose rates until this time
        self._face_lost_at = None               # set while the face is missing
        self._rapid_spikes = deque(maxlen=20)   # timestamps of yaw-rate spikes
        self._glances = deque(maxlen=40)        # timestamps of short side glances
        self._away_since = None
        self._mouth_flips = deque(maxlen=30)    # timestamps of open/close flips
        self._mouth_prev = False
        self._talk_since = None
        self._last_seen = time.time()
        self._last_decay = time.time()
        # dashboard-facing
        self.yaw = 0.0
        self.pitch = 0.0
        self.gaze_label = "CENTER"
        self.direction = "CENTER"
        self.status = "Calibrating..."
        self.last_event = None                  # dict or None
        self.phone_conf = 0.0

    # -- internals ----------------------------------------------------------

    def _calibrated(self):
        return len(self._baseline_yaw) >= BASELINE_FRAMES

    def _base(self):
        if not self._baseline_yaw:
            return 0.0, 0.0
        return (float(np.median(self._baseline_yaw)),
                float(np.median(self._baseline_pitch)))

    def _flip(self, side):
        if LABEL_FLIP:
            return {"LEFT": "RIGHT", "RIGHT": "LEFT"}.get(side, side)
        return side

    def _mark(self, key, active, now, confidence=0.9, extra_label=None):
        """Core temporal gate.

        An event is CONFIRMED once it has been continuously true for min_s.
        It scores/alerts exactly ONCE per occurrence (edge-triggered): the
        condition must clear for RESET_CLEAR_S before it can alert again.
        A behaviour that simply persists escalates on the much slower
        ESCALATE_EVERY_S cadence rather than re-firing every cooldown.

        Returns True while the event is CONFIRMED.
        """
        ev = self.events[key]
        spec = EVENTS[key]

        if not active:
            # Require a short clear period before re-arming, so detector
            # flicker can't be read as the behaviour stopping and restarting.
            if ev.clear_since is None:
                ev.clear_since = now
            elif now - ev.clear_since >= RESET_CLEAR_S:
                ev.active_since = None
                ev.fired = False
            return False

        ev.clear_since = None
        if ev.active_since is None:
            ev.active_since = now
        sustained = now - ev.active_since
        if sustained < max(spec["min_s"], GLANCE_IGNORE_S):
            return False

        if ev.fired:
            # Already alerted for this occurrence. Escalate only if the
            # behaviour keeps going for a long time.
            if now - ev.last_alert < ESCALATE_EVERY_S:
                return True
        elif now - ev.last_alert < spec["cooldown"]:
            # Same event recurring within its cooldown: confirmed but silent
            return True

        ev.last_alert = now
        ev.fired = True
        self.score = min(SCORE_MAX, self.score + spec["points"])
        self.last_event = {
            "type": key,
            "label": extra_label or spec["label"],
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "duration": round(sustained, 1),
            "confidence": round(confidence * 100),
            "points": spec["points"],
        }
        self.alerts.appendleft(dict(self.last_event))
        return True

    # -- main update --------------------------------------------------------

    def update(self, obs, phone_conf, now):
        """obs: FaceObservation or None (face not found for this student).
        phone_conf: max confidence of a phone attributed to this student."""
        self._last_seen = now

        # score decay for clean time
        dt = now - self._last_decay
        self._last_decay = now
        self.score = max(0.0, self.score - SCORE_DECAY_PER_S * dt)

        self.phone_conf = float(phone_conf)
        phone_ok = phone_conf >= CONF_PHONE
        self._mark("PHONE_VISIBLE", phone_ok, now,
                   confidence=phone_conf if phone_ok else 0.9)

        if obs is None:
            if self._face_lost_at is None:
                self._face_lost_at = now
            self._mark("FACE_MISSING", True, now)
            self.status = "Face not visible"
            self.direction = "NO_FACE"
            self.gaze_label = "UNKNOWN"
            return self.snapshot()
        self._mark("FACE_MISSING", False, now)

        # ---- smooth pose (high responsiveness with lightweight smoothing) ----
        a = 0.70
        self._yaw_ema = obs.yaw if self._yaw_ema is None else (1 - a) * self._yaw_ema + a * obs.yaw
        self._pitch_ema = obs.pitch if self._pitch_ema is None else (1 - a) * self._pitch_ema + a * obs.pitch

        # Rapid-movement detection from yaw rate. Skipped for a moment after
        # (re)acquiring a face: the first frames jump wildly as the tracker
        # locks on, which previously produced phantom alerts.
        if self._face_lost_at is not None:
            self._settle_until = now + ACQUIRE_SETTLE_S
            self._face_lost_at = None
            self._prev_yaw = None
        if (self._prev_yaw is not None and self._prev_t is not None
                and now >= self._settle_until):
            dtp = max(1e-3, now - self._prev_t)
            rate = abs(obs.yaw - self._prev_yaw) / dtp
            if rate > RAPID_YAW_DEG_S:
                self._rapid_spikes.append(now)
        self._prev_yaw, self._prev_t = obs.yaw, now
        while self._rapid_spikes and now - self._rapid_spikes[0] > 10.0:
            self._rapid_spikes.popleft()
        self._mark("RAPID_MOVEMENT", len(self._rapid_spikes) >= 3, now)

        # ---- baseline calibration ----
        if not self._calibrated():
            # only learn baseline from near-frontal frames
            if abs(obs.yaw) < 35 and abs(obs.pitch) < 30:
                self._baseline_yaw.append(obs.yaw)
                self._baseline_pitch.append(obs.pitch)
            self.status = "Calibrating..."
            self.yaw, self.pitch = round(self._yaw_ema, 1), round(self._pitch_ema, 1)
            self.direction = "CENTER"
            self.gaze_label = "CENTER"
            return self.snapshot()

        base_yaw, base_pitch = self._base()
        yaw_adj = self._yaw_ema - base_yaw
        pitch_adj = self._pitch_ema - base_pitch
        self.yaw, self.pitch = round(yaw_adj, 1), round(pitch_adj, 1)

        # ---- gaze label ----
        # Smooth with high responsiveness (0.70 EMA)
        g = 0.70
        self._gh_ema = obs.gaze_h if self._gh_ema is None else (1 - g) * self._gh_ema + g * obs.gaze_h
        self._gv_ema = obs.gaze_v if self._gv_ema is None else (1 - g) * self._gv_ema + g * obs.gaze_v

        if len(self._gv_baseline) < BASELINE_FRAMES:
            self._gv_baseline.append(obs.gaze_v)
        gv_base = float(np.median(self._gv_baseline)) if self._gv_baseline else 0.5
        gv_rel = self._gv_ema - gv_base + 0.5

        raw_gaze = "CENTER"
        if self._gh_ema < GAZE_SIDE_LO:
            raw_gaze = self._flip("LEFT")
        elif self._gh_ema > GAZE_SIDE_HI:
            raw_gaze = self._flip("RIGHT")
        elif gv_rel < GAZE_V_UP:
            raw_gaze = "UP"
        elif gv_rel > GAZE_V_DOWN:
            raw_gaze = "DOWN"

        self._gaze_votes.append(raw_gaze)
        counts = {}
        for v in self._gaze_votes:
            counts[v] = counts.get(v, 0) + 1
        best, n_best = max(counts.items(), key=lambda kv: kv[1])
        if n_best >= max(3, len(self._gaze_votes) // 2 + 1):
            self.gaze_label = best
        gaze = self.gaze_label

        # ---- combined direction (head pose dominant, gaze assists) ----
        direction = "CENTER"
        side = None
        if abs(yaw_adj) >= YAW_BEHIND_DEG:
            direction = "BEHIND"
        elif abs(yaw_adj) >= YAW_AWAY_DEG:
            side = "LEFT" if yaw_adj > 0 else "RIGHT"
            direction = self._flip(side)
        elif abs(yaw_adj) >= YAW_ASSIST_DEG and gaze in ("LEFT", "RIGHT"):
            # moderate head turn + gaze in the same direction
            head_side = self._flip("LEFT" if yaw_adj > 0 else "RIGHT")
            if gaze == head_side:
                direction = head_side
        elif pitch_adj >= PITCH_DOWN_DEG or (abs(yaw_adj) < YAW_ASSIST_DEG and gaze == "DOWN" and pitch_adj > PITCH_DOWN_DEG * 0.6):
            direction = "DOWN"
        elif pitch_adj <= -PITCH_UP_DEG:
            direction = "UP"
        self.direction = direction

        # ---- glance repetition tracking ----
        away = direction in ("LEFT", "RIGHT", "BEHIND")
        if away:
            if self._away_since is None:
                self._away_since = now
        else:
            if self._away_since is not None:
                dur = now - self._away_since
                if GLANCE_MIN_S <= dur <= GLANCE_MAX_S:
                    self._glances.append(now)
                self._away_since = None
        while self._glances and now - self._glances[0] > REPEAT_WINDOW_S:
            self._glances.popleft()
        self._mark("REPEATED_GLANCES", len(self._glances) >= REPEAT_GLANCES_N, now,
                   extra_label=f"{len(self._glances)} side glances in a minute")

        # ---- sustained events ----
        self._mark("LOOKING_BEHIND", direction == "BEHIND", now)
        self._mark("LOOKING_AWAY", direction in ("LEFT", "RIGHT"), now,
                   extra_label=f"Sustained look {direction}" if direction in ("LEFT", "RIGHT") else None)
        self._mark("LOOKING_DOWN", direction == "DOWN", now)
        self._mark("LOOKING_UP", direction == "UP", now)

        # ---- talking (mouth open/close oscillation, no audio) ----
        if obs.mouth_open != self._mouth_prev:
            self._mouth_flips.append(now)
            self._mouth_prev = obs.mouth_open
        while self._mouth_flips and now - self._mouth_flips[0] > TALK_WINDOW_S:
            self._mouth_flips.popleft()
        talking = len(self._mouth_flips) >= TALK_TRANSITIONS
        self._mark("TALKING", talking, now)

        # ---- status string ----
        if phone_ok:
            self.status = f"Phone detected ({phone_conf:.0%})"
        elif direction == "BEHIND":
            self.status = "Looking behind"
        elif direction in ("LEFT", "RIGHT"):
            self.status = f"Looking {direction.lower()}"
        elif direction == "DOWN":
            self.status = "Looking down"
        elif direction == "UP":
            self.status = "Looking up"
        elif talking:
            self.status = "Talking"
        else:
            self.status = "Attentive"

        return self.snapshot()

    def snapshot(self):
        trust = max(0.0, min(100.0, round(100.0 - self.score, 1)))
        return {
            "id": self.sid,
            "name": self.name,
            "status": self.status,
            "suspicion_score": round(self.score, 1),
            "risk_score": round(self.score, 1),   # backward-compat for old UI
            "trust_score": trust,                 # real trust score (100 - risk)
            "tier": tier_for(self.score),
            "yaw": self.yaw,
            "pitch": self.pitch,
            "gaze": self.gaze_label,
            "direction": self.direction,
            "phone_conf": round(self.phone_conf * 100),
            "last_event": self.last_event,
            "alerts": list(self.alerts)[:12],
            "calibrated": self._calibrated(),
        }


class RoomBehavior:
    """Room-level temporal events: unknown persons, camera blocked."""

    def __init__(self):
        self.b = StudentBehavior("ROOM", "Room")

    def update(self, unknown_count, frame_gray_std, now):
        self.b._last_decay = now  # room score unused; only events matter
        confirmed_extra = self.b._mark("EXTRA_PERSON", unknown_count > 0, now,
                                       extra_label=f"{unknown_count} unidentified person(s)")
        blocked = frame_gray_std is not None and frame_gray_std < 8.0
        confirmed_block = self.b._mark("CAMERA_BLOCKED", blocked, now)
        return {
            "extra_person": confirmed_extra,
            "camera_blocked": confirmed_block,
            "last_event": self.b.last_event,
            "alerts": list(self.b.alerts)[:12],
        }
