import cv2
import time
import json
import os
import numpy as np
import psycopg2
from flask import Flask, Response, jsonify, send_from_directory, request
from flask_cors import CORS
from datetime import datetime
import base64

# ---------------- CONFIG ----------------
DB_URL = "postgresql://neondb_owner:npg_58LHqXDdanEy@ep-young-sea-aotvi360.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)
CORS(app)

@app.route('/')
def serve_index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    if os.path.exists(os.path.join(BASE_DIR, path)):
        return send_from_directory(BASE_DIR, path)
    return "Not Found", 404

# ---------------- AI MODELS ----------------
detector = cv2.FaceDetectorYN.create(
    "models/face_detection_yunet_2023mar.onnx",
    "",
    (320, 320),
    0.9,
    0.3,
    5000
)
recognizer = cv2.FaceRecognizerSF.create(
    "models/face_recognition_sface_2021dec.onnx",
    ""
)

# ---------------- DB INIT ----------------
def init_db():
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                student_id VARCHAR(50) UNIQUE,
                name VARCHAR(100),
                face_encoding JSONB
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS exam_logs (
                id SERIAL PRIMARY KEY,
                student_id VARCHAR(50),
                risk_score INT,
                direction VARCHAR(50),
                status VARCHAR(50),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Error initializing DB: {e}")

init_db()

# Load registered students into memory for fast comparison
registered_students = [] # list of dicts: {'student_id': str, 'name': str, 'encoding': np.ndarray}

def load_students():
    global registered_students
    registered_students = []
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT student_id, name, face_encoding FROM students;")
        rows = cursor.fetchall()
        for row in rows:
            encoding = np.array(row[2], dtype=np.float32)
            registered_students.append({
                "student_id": row[0],
                "name": row[1],
                "encoding": encoding
            })
        cursor.close()
        conn.close()
        print(f"Loaded {len(registered_students)} students from DB.")
    except Exception as e:
        print(f"Error loading students: {e}")

load_students()

# ---------------- ENDPOINTS ----------------

@app.route('/api/supervisor_login', methods=['POST'])
def supervisor_login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if username == 'admin' and password == 'admin': # Simple hardcoded admin credentials
        return jsonify({"success": True, "token": "super_secret_admin_token_123"})
    else:
        return jsonify({"error": "Invalid credentials"}), 401

@app.route('/api/register', methods=['POST'])
def register():
    # ... existing register logic ...
    data = request.json
    student_id = data.get('student_id')
    name = data.get('name')
    image_b64 = data.get('image')

    if not student_id or not name or not image_b64:
        return jsonify({"error": "Missing fields"}), 400

    # decode base64
    if ',' in image_b64:
        image_b64 = image_b64.split(',')[1]
    img_data = base64.b64decode(image_b64)
    nparr = np.frombuffer(img_data, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if frame is None:
        return jsonify({"error": "Invalid image"}), 400

    detector.setInputSize((frame.shape[1], frame.shape[0]))
    _, faces = detector.detect(frame)

    if faces is None or len(faces) == 0:
        return jsonify({"error": "No face detected"}), 400
    if len(faces) > 1:
        return jsonify({"error": "Multiple faces detected. Please ensure only you are in the frame."}), 400

    face = faces[0]
    aligned_face = recognizer.alignCrop(frame, face)
    feature = recognizer.feature(aligned_face)

    # Save to DB
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        encoding_json = json.dumps(feature.tolist())
        cursor.execute("""
            INSERT INTO students (student_id, name, face_encoding)
            VALUES (%s, %s, %s)
            ON CONFLICT (student_id) DO UPDATE SET name=EXCLUDED.name, face_encoding=EXCLUDED.face_encoding;
        """, (student_id, name, encoding_json))
        conn.commit()
        cursor.close()
        conn.close()
        
        load_students() # refresh memory
        return jsonify({"success": True, "message": "Registered successfully!"})
    except Exception as e:
        print(f"Error registering student: {e}")
        return jsonify({"error": "Database error"}), 500

# ---------------- STATE ----------------
# Track state of the room globally
room_state = {
    "unknown_count": 0,
    "status": "NORMAL"
}

# tracked_students dictionary: { "STU-1002": {"name": "John", "risk_score": 0, "status": "Active", "last_seen": time.time()} }
tracked_students = {}

def log_to_db(student_id, risk_score, direction, status):
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO exam_logs (student_id, risk_score, direction, status)
            VALUES (%s, %s, %s, %s)
        """, (student_id, risk_score, direction, status))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error logging to DB: {e}")

# ---------------- VIDEO PROCESSING ----------------

def gen_frames():
    cap = cv2.VideoCapture(0)
    last_log_time = 0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        now = time.time()
        detector.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = detector.detect(frame)

        current_students_in_frame = set()
        unknown_count = 0

        if faces is not None:
            for face in faces:
                x, y, w, h = map(int, face[:4])
                
                # Extract features
                aligned_face = recognizer.alignCrop(frame, face)
                feature = recognizer.feature(aligned_face)
                
                # Match
                best_match = None
                best_score = 0
                for s in registered_students:
                    score = recognizer.match(feature, s["encoding"], cv2.FaceRecognizerSF_FR_COSINE)
                    if score > best_score:
                        best_score = score
                        best_match = s

                # Threshold for Cosine is ~0.363
                if best_match and best_score >= 0.363:
                    sid = best_match['student_id']
                    name = best_match['name']
                    label = f"{name} ({sid})"
                    color = (0, 255, 0)
                    current_students_in_frame.add(sid)
                    
                    if sid not in tracked_students:
                        tracked_students[sid] = {"name": name, "risk_score": 0, "status": "Active"}
                    
                    tracked_students[sid]["last_seen"] = now
                    # Decrease risk score if they are looking at the screen
                    tracked_students[sid]["risk_score"] = max(0, tracked_students[sid]["risk_score"] - 1)
                    tracked_students[sid]["status"] = "Active"
                else:
                    label = "UNKNOWN"
                    color = (0, 0, 255)
                    unknown_count += 1

                # Draw
                cv2.rectangle(frame, (x, y), (x+w, y+h), color, 2)
                cv2.putText(frame, label, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

        # Update offline students
        for sid in list(tracked_students.keys()):
            if sid not in current_students_in_frame:
                time_away = now - tracked_students[sid].get("last_seen", 0)
                if time_away > 2.0: # Away for more than 2 seconds
                    tracked_students[sid]["risk_score"] = min(100, tracked_students[sid]["risk_score"] + 2)
                    tracked_students[sid]["status"] = "Away"
                if time_away > 60.0: # Remove from tracker if away for 60 seconds
                    del tracked_students[sid]

        # Update room state
        global room_state
        room_state["unknown_count"] = unknown_count
        
        status = "NORMAL"
        if unknown_count > 0:
            status = "HIGH RISK" # Unknown person in room
            
        room_state["status"] = status

        if status != "NORMAL" and now - last_log_time > 5:
            log_to_db("ROOM", 100 if status == "HIGH RISK" else 50, "N/A", status)
            last_log_time = now

        # Encode frame
        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

    cap.release()

@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/status')
def api_status():
    global room_state, tracked_students
    
    students_list = []
    for sid, data in tracked_students.items():
        students_list.append({
            "id": sid,
            "name": data["name"],
            "risk_score": data["risk_score"],
            "status": data["status"]
        })
        
    return jsonify({
        "room_status": room_state["status"],
        "unknown_count": room_state["unknown_count"],
        "students": students_list
    })

@app.route('/api/alerts')
def api_alerts():
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT risk_score, direction, status, timestamp 
            FROM exam_logs 
            ORDER BY timestamp DESC 
            LIMIT 15;
        """)
        rows = cursor.fetchall()
        alerts = []
        for row in rows:
            alerts.append({
                "risk_score": row[0],
                "direction": row[1],
                "status": row[2],
                "timestamp": row[3].strftime("%H:%M:%S")
            })
        cursor.close()
        conn.close()
        return jsonify(alerts)
    except Exception as e:
        print(f"Error fetching alerts: {e}")
        return jsonify([])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
