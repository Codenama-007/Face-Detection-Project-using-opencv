# ProctorAI — Detection Pipeline

Technical reference for the AI exam-monitoring pipeline.

## Architecture

```
Frame
  ├─> YOLO11n ────────────> persons (tracking) + phones (alerts)
  ├─> FaceLandmarker ─────> 478 landmarks, head-pose matrix, blendshapes
  │        ├─> head pose (yaw / pitch / roll, degrees)
  │        ├─> eye gaze  (from trained eyeLook* blendshapes)
  │        └─> mouth     (jawOpen blendshape -> talking)
  ├─> DeepSort ───────────> stable per-student track IDs
  ├─> SFace recognition ──> track ID -> student identity (5-vote lock)
  ├─> TemporalBehaviorEngine ──> events confirmed over time, never one frame
  └─> Suspicion score ────> LOW / MEDIUM / HIGH / CRITICAL -> dashboard
```

Files:

| File | Role |
|---|---|
| `proctor_ai.py` | All detection logic: `FaceAnalyzer`, `StudentBehavior`, `RoomBehavior`. Self-contained and testable. |
| `server.py` | Flask app, camera worker thread, MJPEG stream, REST API. |
| `main.js` / `monitoring.html` | Live dashboard. |

## Head pose

Derived from MediaPipe's **facial transformation matrix**, not a hand-rolled
`solvePnP`. This matters: the 6-point solvePnP approach that was tried first
measured a **stationary** head at yaw −25° to −47° (standard deviation 4.7°),
which flagged a motionless user as CRITICAL. The model's own matrix measures
the same pose at **+0.7° mean, sd 1.0°**.

Each student gets a **personal baseline**: the median of their first 45
near-frontal frames. All thresholds are relative to that baseline, so a student
who naturally sits at a slight angle is not permanently flagged.

## Eye gaze

Taken from the model's trained `eyeLook{In,Out,Up,Down}{Left,Right}`
blendshapes rather than hand-computed iris ratios. Blinks
(`eyeBlink* > 0.5`) neutralise vertical gaze, since a blink otherwise reads as
looking down.

Vertical gaze is also baselined per student: on a laptop the camera sits below
the screen, so "looking at the screen" is genuinely "looking up".

Gaze is EMA-smoothed and then majority-voted over 5 frames. Raw
frame-to-frame labels flickered constantly; after smoothing, label changes are
**2% of samples** when a user sits still.

## Temporal behaviour engine — the core of false-positive control

**No alert is ever produced from a single frame.**

An event must be continuously true for its `min_s` before it is confirmed:

| Event | Points | Must last |
|---|---|---|
| Phone visible | 100 | 2.0 s |
| Another person in frame | 80 | 3.0 s |
| Camera blocked | 60 | 2.0 s |
| Looking behind | 45 | 2.0 s |
| Face missing | 40 | 3.0 s |
| Face covered / occluded | 35 | 3.0 s |
| Sustained side look | 20 | 5.0 s |
| Looking down | 15 | 6.0 s |
| Rapid head movement | 15 | 3 spikes / 10 s |
| Looking up | 10 | 6.0 s |
| Talking | 10 | 4.0 s |
| Repeated side glances | 25 | 8 glances / 60 s |

### Edge-triggered, not level-triggered

Each occurrence alerts **once**. The condition must clear for
`RESET_CLEAR_S` (1.5 s) before it can alert again. A behaviour that simply
persists escalates on a slow `ESCALATE_EVERY_S` (45 s) cadence.

This fixed a measured bug: one continuous look-away previously re-fired every
8 seconds at +20 points each, driving a score to 180 (CRITICAL) — 8 alerts for
a single behaviour. It now produces **2** over 60 seconds, while three genuinely
separate look-aways still produce three alerts.

The 1.5 s clear requirement also means detector flicker cannot be misread as
the behaviour stopping and restarting: a single-frame dropout every 5 seconds
across a 50-second look-away yields **1** alert, not 10.

### Explicitly not flagged

Blinks, brief glances (< 0.3 s), natural posture shifts, writing, reading, and
small eye movements. Only sustained or repeated behaviour scores.

## Suspicion score

Points accumulate per confirmed event and **decay at 1.0 point/second** of
clean behaviour, capped at 100.

| Score | Tier |
|---|---|
| 0–19 | LOW |
| 20–49 | MEDIUM |
| 50–79 | HIGH |
| 80+ | CRITICAL |

The output is always a graded score, never a binary "cheating" verdict — the
system flags behaviour for human review.

## Phone detection

Handled by `phone_detect.py` on its own thread.

### What the measurements actually showed

Evaluated on COCO val2017: 120 images containing phones, and 120 phone-free
images containing books, remotes, laptops, keyboards, TVs and mice.

The suspicion that the detector "flags any rectangular thing as a phone" was
**not reproduced**. At the old settings it produced **zero** false positives
across all 120 phone-free images. Its actual defect was the opposite — it
found only **5.5% of small/distant phones**:

| Config | Recall | Distant-phone recall | False pos | Precision |
|---|---|---|---|---|
| yolo11n @480 conf .60 (old) | 18.9% | **5.5%** | 0 | 100% |
| yolo11s @640 conf .25 | 48.6% | 38.5% | 6 | 92.3% |
| yolo11s ROI conf .40 | 52.7% | 44.0% | 5 | 94.0% |
| yolo11m ROI conf .40 | 64.2% | 57.1% | 3 | 96.9% |

Where the remaining false positives land: `background`, `remote`, and one
`toothbrush` — never a book, laptop, keyboard or TV.

### Person-ROI detection — why distant phones are now found

Each person box is cropped, padded by 18% (a concealed phone often sits just
outside the torso — in a lap, below the desk edge) and re-detected on its own.
A phone 20px wide in the full frame becomes ~100px wide once a person crop is
scaled to the 640px network input. That is what makes a phone held low or
cupped in a hand detectable at all.

The whole-frame pass still contributes most of the recall (ROI-only drops
yolo11s from 52.7% to 31.1%), so it runs every pass; the interval is what
keeps the cost affordable.

### Three independent layers keep precision high

1. **Plausibility filter** — rejects anything too large relative to its
   holder, extreme aspect ratios, and specks. Unit-tested to reject a
   laptop-sized rectangle and a sliver while accepting a real phone. Measured
   to cost **zero** recall. COCO never triggered it, so its value against a
   specific scene object is unverified — it is a guard, not a proven fix.
2. **Attribution** — a phone is tied to the person whose padded box contains it.
3. **The temporal gate** — a phone must persist ~2s before an alert. This is
   what turns per-frame precision (94–97%) into alert-level precision, since
   isolated single-frame detections never survive it.

### Tuning

| Env var | Default | Effect |
|---|---|---|
| `PHONE_MODEL` | `yolo11s.pt` | `yolo11m.pt` = +12% recall, ~2x slower |
| `PHONE_DETECTION` | `on` | `off` disables the thread entirely |

`PHONE_CONF` (0.40) in `phone_detect.py` is deliberately below the old 0.60:
at 0.60 the detector missed 94% of distant phones. Precision is recovered by
the layers above rather than by a blunt threshold.

## Face identification (who is this student?)

Handled by `face_recog.py`, on its own thread so it never slows the video.

| Stage | Model |
|---|---|
| Detection | SCRFD-10G (`det_10g.onnx`) |
| Alignment | 5-point Umeyama similarity transform |
| Embedding | ArcFace R50 trained on WebFace600K (`w600k_r50.onnx`), 512-d |
| Matching | best-of-N templates per student, cosine |

### Why the old stack was replaced

The previous YuNet + SFace pairing was measured scoring the enrolled subject
at **0.393 against their own template — below its own 0.45 accept threshold**.
It was effectively failing to recognise the person it had enrolled. The cause
was single-template enrolment: one photo captures one pose under one light,
and any deviation collapses the score.

### Multi-template enrolment

Enrolment now captures ~18 frames across six guided head positions and stores
one template per usable frame. Matching takes the best score over a student's
templates. Near-duplicate frames (cosine > 0.985) are dropped, and if a
student exceeds `MAX_TEMPLATES` the set is pruned by farthest-point selection
so the kept templates stay diverse.

**This is the single biggest accuracy factor** — bigger than the model change.

### Measured accuracy: 1 subject in a crowd of 100

Gallery of 101 identities: the enrolled subject plus **100 real people** from
Labeled Faces in the Wild.

| | Result |
|---|---|
| Held-out probes identified correctly | **12/12** |
| Genuine score | 0.741 mean, 0.544 min |
| Best impostor score | 0.188 max |
| Separation margin | **+0.357** |
| False accepts at threshold 0.36 | **0 / 100** |

Against distance, still inside that 100-person crowd:

| Face width | Genuine | Best impostor | Margin |
|---|---|---|---|
| 160 px | 0.831 | 0.144 | +0.687 |
| 80 px | 0.790 | 0.174 | +0.616 |
| 45 px | 0.693 | 0.146 | +0.547 |
| 28 px | 0.606 | 0.090 | +0.516 |

Against low light (with CLAHE + adaptive gamma applied first):

| Light level | Genuine | Best impostor | Result |
|---|---|---|---|
| 100% | 0.794 | 0.132 | identified |
| 60% | 0.679 | 0.150 | identified |
| 40% | 0.620 | 0.097 | identified |
| 25% | 0.560 | 0.093 | identified |

### Physical limits — where it genuinely stops working

These are information limits, not tuning problems:

* **Below ~16–20 px of face width** the self-match score falls under the
  accept threshold (0.346 at 16 px). On a 1280-wide webcam a face is ~16 px
  at roughly 8–10 m. Recognising a face across a large hall needs more
  sensor resolution — an optical zoom or a higher-resolution camera — not a
  different model.
* **Below ~15% illumination** neither detector finds a face at all, so
  recognition never runs. Sensor noise dominates the signal. The fix is
  light: IR illumination and an IR-capable camera.

Quality gates (`MIN_FACE_PX`, `MIN_BRIGHTNESS`, `MIN_SHARPNESS`) exist to
report UNCERTAIN rather than force a wrong identity. They are set
deliberately permissive: an earlier stricter sharpness gate was measured
rejecting 60 px faces that were scoring 0.739 with a +0.623 margin, i.e.
discarding certain-correct identifications. The score margin, not a blur
metric, is the real confidence signal.

## Performance

Measured per-stage cost on this machine (CPU only, 960x540 frame):

| Stage | Cost | Runs |
|---|---|---|
| YOLO11n @ imgsz 480 | 36 ms | every AI frame |
| DeepSort (torch re-ID) | 28 ms | every AI frame |
| MediaPipe FaceLandmarker | 5 ms | every AI frame |
| JPEG encode | 2 ms | every streamed frame |
| SCRFD detect | 178 ms | identification thread only |
| ArcFace embed (flip TTA) | 172 ms | identification thread only |

Measured worker costs (same machine, one person in frame):

| Configuration | FPS |
|---|---|
| Face ID off, phone off | 7.0 |
| Face ID **on**, phone off | 3.6 |
| Face ID off, phone **on** | 5.2 |
| Both on, before the backoff fix | 2.4 |
| **Both on, after the backoff fix** | **11.6** |

The backoff fix matters more than any model choice. A track that cannot be
identified — someone not enrolled, or a spurious detection — used to pin the
identifier at its 0.5s fast cadence permanently, roughly halving the frame
rate for the entire session. It now gives up on a track after
`ID_MAX_ATTEMPTS` passes and retries it only every `ID_RETRY_AFTER` seconds,
so the system settles onto the 3s slow cadence.

Absolute FPS varies with machine load and thermal state; the relative costs
above are the reliable signal.

ArcFace embedding was originally 729 ms. Almost all of it was the face
alignment step using `cv2.estimateAffinePartial2D` with `LMEDS` — a robust
iterative estimator applied to 5 exact correspondences that contain no
outliers. Replacing it with a closed-form Umeyama solve cut alignment from
~280 ms to 0.24 ms and left the resulting embedding **bit-identical**
(cosine 1.0000 against the old alignment).

Tunables at the top of the video section in `server.py`:

| Constant | Default | Effect |
|---|---|---|
| `PROCESS_EVERY` | 3 | Run AI on every Nth frame; raise to go faster |
| `YOLO_IMGSZ` | 480 | 320 is ~2x faster but starts missing phones |
| `MAX_STREAM_WIDTH` | 960 | Downscale large CCTV frames before processing |
| `ID_INTERVAL_FAST` | 0.5 s | Identification cadence while someone is unknown |
| `ID_INTERVAL_SLOW` | 3.0 s | Identification cadence once everyone is known |

Reaching 25–30 FPS on this CPU means giving something up: `YOLO_IMGSZ=320`
(weaker phone detection) or `PROCESS_EVERY=4`. A CUDA GPU removes the
trade-off entirely — install CUDA-enabled torch and onnxruntime-gpu and both
YOLO and ArcFace move to the GPU automatically.

One camera worker thread owns the capture and runs the AI once; every viewer
reads the latest encoded frame, so extra dashboard tabs cost almost nothing.
For GPU: install CUDA-enabled torch and ultralytics will use it automatically.

## Camera ownership

A local webcam can only be held by one process. The worker acquires the camera
only while someone is watching `/video_feed`, and releases it after
`IDLE_RELEASE_SECONDS`. The enrollment page uses the **browser's**
`getUserMedia`, so it calls `POST /api/camera/pause` to make the server let go,
and `POST /api/camera/resume` on unload.

## Tuning

All thresholds are constants at the top of `proctor_ai.py`.

- Too many alerts → raise `YAW_AWAY_DEG`, raise each event's `min_s`, or lower
  `SCORE_DECAY_PER_S`.
- Left/right reversed for your camera → set `LABEL_FLIP = True`.
- Stricter phone detection → raise `CONF_PHONE` and `EVENTS["PHONE_VISIBLE"]["min_s"]`.

## Re-enrolment required after the recognition upgrade

ArcFace produces 512-d embeddings; the old SFace ones were 128-d and are not
comparable. Students enrolled before the upgrade are kept in the database but
**will not be recognised until re-enrolled**. The server prints exactly who
is affected at startup. Re-enrol from the enrollment page — it now runs the
guided multi-angle capture.

## Benchmarks

Each is runnable and prints the numbers quoted above.

| Script | Measures |
|---|---|
| `bench_recognition.py` | Baseline SFace scores vs distance and light |
| `bench_recognition2.py` | A/B: SFace single-template vs ArcFace multi-template |
| `bench_detector.py` | YuNet vs SCRFD detection limits |
| `bench_crowd.py` | 1-in-100 identification using real LFW faces |
| `bench_gate.py` | Confirms quality gates reject nothing recognisable |
| `bench_stages.py` | Per-stage timing profile |
| `bench_phone.py` | Phone recall/false-positives across model sizes on COCO |
| `bench_phone2.py` | Confidence sweep + person-ROI comparison |
| `bench_phone3.py` | Final detector vs old config, with filter ablation |
| `bench_phone4.py` | ROI-only model comparison (speed/accuracy dial) |

The phone benchmarks need COCO val2017 in `datasets/`:

```bash
curl -o datasets/val2017.zip http://images.cocodataset.org/zips/val2017.zip
```

## Not implemented

Custom YOLO fine-tuning on classroom/phone datasets, and TensorRT export, are
**not** included — both need training data and a GPU this deployment does not
have. Phone detection still uses pretrained COCO weights. Fine-tuning remains
the main further gain for phones specifically, especially partially hidden or
lap-held ones.

No liveness/anti-spoofing check is implemented: a printed photo or a phone
screen held up to the camera would currently be accepted. That matters for a
proctoring product and would need a dedicated anti-spoof model.
