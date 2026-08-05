import cv2
import mediapipe as mp
import time
import json
import os

# ---------------- STATE ----------------
look_start_time = None
current_direction = "CENTER"
no_face_start = None

risk = 0

data = {
    "id": 101,
    "name": "Vernon Rodrigues",
    "risk_score": 0,
    "direction": "CENTER",
    "status": "NORMAL"
}

# ---------------- MODEL ----------------
face_detection = mp.solutions.face_detection.FaceDetection(
    model_selection=0,
    min_detection_confidence=0.5
)

cap = cv2.VideoCapture(0)

prev_frame_time = time.time()

# ---------------- LOOP ----------------
while True:
    ret, frame = cap.read()
    if not ret:
        break

    now = time.time()
    frame_dt = now - prev_frame_time
    prev_frame_time = now
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = face_detection.process(rgb)

    direction = "CENTER"

    # ================= FACE DETECTED =================
    if results.detections:

        det = results.detections[0]
        bbox = det.location_data.relative_bounding_box

        face_x = bbox.xmin + bbox.width / 2
        face_y = bbox.ymin + bbox.height / 2

        # -------- direction logic --------
        # Labels are from the student's point of view: a face on the
        # left side of the (non-mirrored) camera image means the
        # student moved to THEIR right.
        if face_x < 0.4:
            direction = "RIGHT"
        elif face_x > 0.6:
            direction = "LEFT"
        elif face_y < 0.4:
            direction = "TOP"
        elif face_y > 0.6:
            direction = "BOTTOM"
        else:
            direction = "CENTER"

        # -------- reset no-face timer --------
        no_face_start = None

        # -------- direction change logic --------
        if direction != current_direction:
            current_direction = direction
            look_start_time = now

        else:
            # risk increases gradually: 2 points per second of
            # continuously looking away (per-frame delta, not the
            # accumulated duration, so growth stays linear)
            if current_direction != "CENTER":
                risk += frame_dt * 2
                risk = min(risk, 100)

    # ================= NO FACE =================
    else:
        direction = "NO_FACE"
        current_direction = "CENTER"
        look_start_time = None

        if no_face_start is None:
            no_face_start = now

        # after a 2 second grace period, 5 risk points per second
        if now - no_face_start > 2:
            risk += frame_dt * 5
            risk = min(risk, 100)

    # ---------------- STATUS ----------------
    if risk < 10:
        status = "NORMAL"
    elif risk < 30:
        status = "SUSPICIOUS"
    else:
        status = "HIGH RISK"

    # ---------------- UPDATE DICT ----------------
    data["risk_score"] = int(risk)
    data["direction"] = current_direction
    data["status"] = status


    # Write to a temp file, then atomically rename so the dashboard
    # never reads a half-written JSON file
    tmp_file = "snapshot_tmp.json"
    final_file = "snapshot.json"

    with open(tmp_file, "w") as f:
        json.dump(data, f)

    os.replace(tmp_file, final_file)

    # ---------------- DISPLAY ----------------
    cv2.putText(frame, f"Direction: {current_direction}", (30, 50),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 2)

    cv2.putText(frame, f"Risk: {int(risk)}", (30, 100),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

    cv2.putText(frame, f"Status: {status}", (30, 150),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    cv2.imshow("Exam Monitor", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()