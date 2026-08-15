import cv2
import time
import json
import os
import numpy as np
import psycopg2
from flask import Flask, Response, jsonify, send_from_directory, request, session, redirect, url_for
from flask_cors import CORS
from datetime import datetime
import base64
from ultralytics import YOLO
from deep_sort_realtime.deepsort_tracker import DeepSort

# ---------------- CONFIG ----------------
DB_URL = "postgresql://neondb_owner:npg_58LHqXDdanEy@ep-young-sea-aotvi360.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)
app.secret_key = 'super_secret_proctor_key_change_in_production'
CORS(app, supports_credentials=True)

# ---------------- MIDDLEWARE ----------------
@app.before_request
def require_auth():
    # Only protect API endpoints, video feed, monitoring, and enrollment.
    # We do NOT protect the index, login page, static assets, or the login API endpoint itself.
    protected_html = ['/monitoring.html', '/enrollment.html']
    
    # Allow login endpoints and static files
    if request.endpoint in ['supervisor_login', 'serve_index']:
        return

    path = request.path
    if path in protected_html or path.startswith('/video_feed') or (path.startswith('/api/') and path != '/api/supervisor_login'):
        if not session.get('admin_logged_in'):
            # Return 401 for API, redirect to login for HTML pages
            if path.startswith('/api/') or path.startswith('/video_feed'):
                return jsonify({"error": "Unauthorized"}), 401
            else:
                return redirect('/supervisor_login.html')

@app.route('/')
def serve_index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    if os.path.exists(os.path.join(BASE_DIR, path)):
        return send_from_directory(BASE_DIR, path)
    return "Not Found", 404

# ---------------- AI MODELS ----------------
yolo_model = YOLO('yolov8s.pt')
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

# DeepSort Tracker
tracker = DeepSort(max_age=30)

# Track ID to Student ID mapping
track_to_student = {}
track_votes = {} # track_id -> {student_id: count}
historical_risk_scores = {}
head_pose_buffers = {}
baseline_calibration = {} # sid -> {"nx": [], "ny": []}
VIDEO_SOURCE = 0 # Can be an RTSP url like 'rtsp://admin:123@192.168.1.100/stream'

# Session State
SESSION_ACTIVE = False
session_start_time = None

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
        session['admin_logged_in'] = True
        return jsonify({"success": True, "message": "Logged in successfully"})
    else:
        return jsonify({"error": "Invalid credentials"}), 401

@app.route('/api/supervisor_logout', methods=['POST', 'GET'])
def supervisor_logout():
    session.pop('admin_logged_in', None)
    return jsonify({"success": True})

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

@app.route('/reports/<path:filename>')
def download_report(filename):
    return send_from_directory('static/reports', filename)

@app.route('/api/session/status', methods=['GET'])
def get_session_status():
    global SESSION_ACTIVE
    return jsonify({"active": SESSION_ACTIVE})

@app.route('/api/session/start', methods=['POST'])
def start_session():
    global SESSION_ACTIVE, session_start_time, tracked_students, track_to_student
    SESSION_ACTIVE = True
    session_start_time = datetime.now()
    # Reset tracking state for new session
    for sid in tracked_students:
        tracked_students[sid]["risk_score"] = 0
        tracked_students[sid]["status"] = "Active"
    return jsonify({"success": True, "message": "Session started"})

@app.route('/api/session/end', methods=['POST'])
def end_session():
    global SESSION_ACTIVE, session_start_time
    SESSION_ACTIVE = False

    # Generate HTML Report
    import os
    os.makedirs('static/reports', exist_ok=True)
    report_filename = f"report_{datetime.now().strftime('%Y%md_%H%M%S')}.html"
    report_path = os.path.join('static/reports', report_filename)

    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Compute summary stats from real data
    students_snapshot = dict(tracked_students)
    total_students = len(students_snapshot)
    high_risk_count = sum(1 for d in students_snapshot.values() if d['risk_score'] > 75)
    suspicious_count = sum(1 for d in students_snapshot.values() if 25 < d['risk_score'] <= 75)
    avg_risk = round(sum(d['risk_score'] for d in students_snapshot.values()) / total_students, 1) if total_students else 0
    avg_trust = round(100 - avg_risk, 1) if total_students else 0

    if high_risk_count > 0:
        integrity_status = "HIGH RISK"
        integrity_color = "#ef4444"
        integrity_bg = "rgba(239,68,68,0.08)"
        integrity_dot = "#ef4444"
    elif suspicious_count > 0:
        integrity_status = "ATTENTION REQUIRED"
        integrity_color = "#f59e0b"
        integrity_bg = "rgba(245,158,11,0.08)"
        integrity_dot = "#f59e0b"
    else:
        integrity_status = "SECURE"
        integrity_color = "#10b981"
        integrity_bg = "rgba(16,185,129,0.08)"
        integrity_dot = "#10b981"

    # Build student rows
    student_rows_html = ""
    for sid, data in students_snapshot.items():
        score = int(data['risk_score'])
        trust = max(0, 100 - score)
        bar_pct = score
        if score > 75:
            risk_label = "HIGH RISK"
            risk_color = "#ef4444"
            risk_bg = "rgba(239,68,68,0.12)"
            bar_color = "#ef4444"
        elif score > 25:
            risk_label = "SUSPICIOUS"
            risk_color = "#f59e0b"
            risk_bg = "rgba(245,158,11,0.12)"
            bar_color = "#f59e0b"
        else:
            risk_label = "LOW RISK"
            risk_color = "#10b981"
            risk_bg = "rgba(16,185,129,0.12)"
            bar_color = "#10b981"

        status_txt = data.get('status', 'N/A')

        student_rows_html += f"""
                <tr>
                    <td style="font-family:monospace;font-size:0.8rem;color:#8899b8;">{sid}</td>
                    <td style="font-weight:600;color:#f0f4ff;">{data['name']}</td>
                    <td>
                        <div style="display:flex;align-items:center;gap:0.6rem;">
                            <div style="flex:1;height:6px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden;min-width:70px;">
                                <div style="width:{bar_pct}%;height:100%;background:{bar_color};border-radius:99px;"></div>
                            </div>
                            <span style="font-size:0.85rem;font-weight:700;color:{risk_color};min-width:32px;">{score}</span>
                        </div>
                    </td>
                    <td style="font-weight:600;color:#10b981;">{trust}%</td>
                    <td>
                        <span style="display:inline-block;padding:0.18rem 0.6rem;border-radius:99px;font-size:0.68rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;background:{risk_bg};color:{risk_color};border:1px solid {risk_color}33;">
                            {risk_label}
                        </span>
                    </td>
                    <td style="font-size:0.8rem;color:#8899b8;">{status_txt}</td>
                </tr>"""

    if not student_rows_html:
        student_rows_html = """
                <tr>
                    <td colspan="6" style="text-align:center;padding:3rem;color:#4b5e7a;">
                        <div style="display:flex;flex-direction:column;align-items:center;gap:0.75rem;">
                            <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" fill="none" stroke="#4b5e7a" stroke-width="1.5" viewBox="0 0 24 24" style="opacity:0.3;"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
                            <div>
                                <div style="font-size:0.85rem;font-weight:600;color:#5c7098;margin-bottom:0.25rem;">No Student Records Available</div>
                                <div style="font-size:0.75rem;line-height:1.5;">No monitored students were recorded during this session.</div>
                            </div>
                        </div>
                    </td>
                </tr>"""

    # AI Insights
    insights = []
    if total_students > 0:
        if high_risk_count > 0:
            insights.append(f"{high_risk_count} student{'s' if high_risk_count > 1 else ''} exceeded the high-risk threshold during this examination session.")
        if suspicious_count > 0:
            insights.append(f"{suspicious_count} student{'s' if suspicious_count > 1 else ''} showed suspicious behavior patterns that may require review.")
        if avg_trust >= 80:
            insights.append(f"Overall examination integrity remained within acceptable limits — average trust score {avg_trust}%.")
        if avg_risk < 20:
            insights.append("Risk levels across all monitored students were within the configured safe range.")
    else:
        insights.append("No students were monitored during this session. Ensure camera and enrollment are configured before starting a session.")

    insights_html = "".join(f'<div style="display:flex;align-items:flex-start;gap:0.6rem;padding:0.6rem 0;border-bottom:1px solid rgba(255,255,255,0.04);"><span style="color:#3b82f6;font-size:0.9rem;margin-top:1px;">›</span><span style="font-size:0.82rem;color:#8899b8;line-height:1.5;">{i}</span></div>' for i in insights)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ProctorAI — Examination Integrity Report</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', -apple-system, sans-serif;
            background: #06090e;
            color: #f8fafc;
            min-height: 100vh;
            -webkit-font-smoothing: antialiased;
            line-height: 1.6;
        }}
        .report-wrap {{
            max-width: 1200px;
            width: calc(100% - 48px);
            margin: 0 auto;
            padding: 2.5rem 0 4rem;
        }}

        /* ── Header ── */
        .r-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            padding: 2rem;
            background: rgba(255,255,255,0.025);
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 18px;
            margin-bottom: 1.5rem;
        }}
        .r-brand h1 {{
            font-size: 1.6rem;
            font-weight: 800;
            letter-spacing: -0.04em;
            background: linear-gradient(135deg, #f0f4ff 0%, #93c5fd 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
            margin-bottom: 0.2rem;
        }}
        .r-brand p {{ font-size: 0.8rem; color: #4b5e7a; letter-spacing: 0.04em; }}
        .r-meta {{ text-align: right; }}
        .r-status-pill {{
            display: inline-flex; align-items: center; gap: 0.4rem;
            padding: 0.3rem 0.8rem; border-radius: 99px;
            background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.25);
            font-size: 0.68rem; font-weight: 700; letter-spacing: 0.08em; color: #10b981;
            text-transform: uppercase; margin-bottom: 0.6rem;
        }}
        .r-status-dot {{
            width: 5px; height: 5px; border-radius: 50%; background: #10b981;
        }}
        .r-meta time {{ display: block; font-size: 0.78rem; color: #8899b8; }}
        .r-meta strong {{ font-size: 0.72rem; font-weight: 600; color: #4b5e7a; letter-spacing: 0.05em; text-transform: uppercase; }}

        /* ── Summary Cards ── */
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 1rem;
            margin-bottom: 1.5rem;
        }}
        .summary-card {{
            background: rgba(255,255,255,0.025);
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 14px;
            padding: 1.25rem 1.5rem;
        }}
        .summary-card .s-label {{ font-size: 0.68rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: #4b5e7a; margin-bottom: 0.4rem; }}
        .summary-card .s-value {{ font-size: 2rem; font-weight: 800; letter-spacing: -0.04em; line-height: 1; }}
        .summary-card .s-sub {{ font-size: 0.72rem; color: #4b5e7a; margin-top: 0.25rem; }}

        /* ── Integrity Status ── */
        .integrity-card {{
            background: {integrity_bg};
            border: 1px solid {integrity_color}33;
            border-radius: 14px;
            padding: 1.5rem 2rem;
            display: flex;
            align-items: center;
            gap: 1.5rem;
            margin-bottom: 1.5rem;
        }}
        .i-indicator {{
            width: 14px; height: 14px; border-radius: 50%;
            background: {integrity_dot};
            box-shadow: 0 0 12px {integrity_dot};
            flex-shrink: 0;
        }}
        .i-label {{ font-size: 0.7rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: #4b5e7a; margin-bottom: 0.2rem; }}
        .i-status {{ font-size: 1.4rem; font-weight: 800; letter-spacing: -0.02em; color: {integrity_color}; }}

        /* ── Section ── */
        .r-section {{
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 14px;
            overflow: hidden;
            margin-bottom: 1.25rem;
        }}
        .r-section-header {{
            padding: 0.9rem 1.5rem;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #8899b8;
        }}
        .r-section-body {{ padding: 0 0; }}

        /* ── Table ── */
        .r-table {{ width: 100%; border-collapse: collapse; }}
        .r-table th {{
            padding: 0.75rem 1.5rem;
            text-align: left;
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #4b5e7a;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }}
        .r-table td {{
            padding: 0.85rem 1.5rem;
            border-bottom: 1px solid rgba(255,255,255,0.04);
            font-size: 0.82rem;
            vertical-align: middle;
        }}
        .r-table tr:last-child td {{ border-bottom: none; }}
        .r-table tbody tr:hover {{ background: rgba(255,255,255,0.02); }}

        /* ── Insights ── */
        .insights-body {{ padding: 0.5rem 1.5rem 1rem; }}

        /* ── Footer ── */
        .r-footer {{
            text-align: center;
            padding-top: 2.5rem;
            color: #2d3e58;
            font-size: 0.75rem;
        }}
        .r-footer strong {{ color: #3b82f6; }}

        /* ── Print ── */
        @media print {{
            body {{ background: #fff !important; color: #111 !important; }}
            .r-header, .r-section, .summary-card, .integrity-card {{
                background: #f8faff !important;
                border-color: #dde3f0 !important;
            }}
            .r-table th {{ color: #555 !important; }}
            .r-table td {{ color: #222 !important; border-color: #e5e9f0 !important; }}
            .r-brand h1 {{ -webkit-text-fill-color: #1e3a5f !important; }}
            @page {{ margin: 2cm; }}
        }}

        @media (max-width: 700px) {{
            .report-wrap {{ width: calc(100% - 24px); }}
            .r-header {{ flex-direction: column; gap: 1rem; }}
            .r-meta {{ text-align: left; }}
            .summary-grid {{ grid-template-columns: 1fr 1fr; }}
            .r-table {{ overflow-x: auto; display: block; }}
        }}
    </style>
</head>
<body>
<div class="report-wrap">

    <!-- Header -->
    <div class="r-header">
        <div class="r-brand">
            <h1>ProctorAI</h1>
            <p>Examination Integrity Report &nbsp;·&nbsp; AI-Powered Security Monitoring</p>
        </div>
        <div class="r-meta">
            <div class="r-status-pill"><span class="r-status-dot"></span>Generated</div>
            <strong>Report Generated</strong>
            <time>{generated_at}</time>
        </div>
    </div>

    <!-- Executive Summary -->
    <div class="summary-grid">
        <div class="summary-card">
            <div class="s-label">Total Students</div>
            <div class="s-value" style="color:#f0f4ff;">{total_students}</div>
            <div class="s-sub">Monitored this session</div>
        </div>
        <div class="summary-card">
            <div class="s-label">High Risk</div>
            <div class="s-value" style="color:#ef4444;">{high_risk_count}</div>
            <div class="s-sub">Risk score &gt; 75</div>
        </div>
        <div class="summary-card">
            <div class="s-label">Suspicious</div>
            <div class="s-value" style="color:#f59e0b;">{suspicious_count}</div>
            <div class="s-sub">Risk score 25 – 75</div>
        </div>
        <div class="summary-card">
            <div class="s-label">Avg Trust Score</div>
            <div class="s-value" style="color:#10b981;">{avg_trust}%</div>
            <div class="s-sub">Across all students</div>
        </div>
        <div class="summary-card">
            <div class="s-label">Avg Risk Score</div>
            <div class="s-value" style="color:#8899b8;">{avg_risk}</div>
            <div class="s-sub">Session average</div>
        </div>
    </div>

    <!-- Integrity Status -->
    <div class="integrity-card">
        <div class="i-indicator"></div>
        <div>
            <div class="i-label">Examination Integrity</div>
            <div class="i-status">{integrity_status}</div>
        </div>
    </div>

    <!-- Student Risk Table -->
    <div class="r-section">
        <div class="r-section-header">Student Risk Summary</div>
        <div class="r-section-body">
            <table class="r-table">
                <thead>
                    <tr>
                        <th>Student ID</th>
                        <th>Name</th>
                        <th>Risk Score</th>
                        <th>Trust Score</th>
                        <th>Risk Level</th>
                        <th>Last Status</th>
                    </tr>
                </thead>
                <tbody>
                    {student_rows_html}
                </tbody>
            </table>
        </div>
    </div>

    <!-- AI Insights -->
    <div class="r-section">
        <div class="r-section-header">AI Security Insights</div>
        <div class="insights-body">
            {insights_html}
        </div>
    </div>

    <!-- Footer -->
    <div class="r-footer">
        <strong>ProctorAI</strong> · AI-Powered Examination Security<br>
        Generated automatically by the ProctorAI monitoring system.
    </div>

</div>
</body>
</html>"""

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    return jsonify({"success": True, "report_url": f"/reports/{report_filename}"})

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
    global tracked_students, current_students_in_frame, track_to_student, track_votes, historical_risk_scores, head_pose_buffers, baseline_calibration, VIDEO_SOURCE, SESSION_ACTIVE
    cap = cv2.VideoCapture(VIDEO_SOURCE)
    last_log_time = 0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        now = time.time()
        
        # 1. YOLO Detection
        yolo_results = yolo_model(frame, stream=True, verbose=False)
        person_detections = []
        phone_detected = False
        book_detected = False
        
        for r in yolo_results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                
                if cls_id == 0 and conf > 0.5: # person
                    person_detections.append(([x1, y1, x2 - x1, y2 - y1], conf, "person"))
                elif cls_id == 67 and conf > 0.65: # phone
                    phone_detected = True
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
                    cv2.putText(frame, f"PHONE: {conf:.2f}", (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 3)
                elif cls_id == 73 and conf > 0.60: # book
                    book_detected = True
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
                    cv2.putText(frame, f"BOOK: {conf:.2f}", (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 3)

        room_state["phone_detected"] = phone_detected
        room_state["book_detected"] = book_detected
        
        # 2. DeepSort Tracking
        tracks = tracker.update_tracks(person_detections, frame=frame)
        
        current_students_in_frame = set()
        unknown_count = 0
        
        for track in tracks:
            if not track.is_confirmed():
                continue
                
            track_id = track.track_id
            tx1, ty1, tx2, ty2 = map(int, track.to_ltrb())
            
            # Bound crop
            tx1 = max(0, tx1)
            ty1 = max(0, ty1)
            tx2 = min(frame.shape[1], tx2)
            ty2 = min(frame.shape[0], ty2)
            
            # Minimum bounding box check
            if tx2 - tx1 < 40 or ty2 - ty1 < 40:
                continue
                
            person_crop = frame[ty1:ty2, tx1:tx2]
            
            # Identify if needed
            if track_id not in track_to_student:
                detector.setInputSize((person_crop.shape[1], person_crop.shape[0]))
                _, faces = detector.detect(person_crop)
                
                if faces is not None and len(faces) > 0:
                    # Sort faces by area (width * height) and pick the largest
                    faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
                    face = faces[0]
                    aligned_face = recognizer.alignCrop(person_crop, face)
                    feature = recognizer.feature(aligned_face)
                    
                    best_match = None
                    best_score = 0
                    for s in registered_students:
                        score = recognizer.match(feature, s["encoding"], cv2.FaceRecognizerSF_FR_COSINE)
                        if score > best_score:
                            best_score = score
                            best_match = s
                    
                    if best_match and best_score >= 0.45:
                        sid = best_match['student_id']
                        if track_id not in track_votes:
                            track_votes[track_id] = {}
                        track_votes[track_id][sid] = track_votes[track_id].get(sid, 0) + 1
                        
                        # Lock identity if 5 votes reached
                        if track_votes[track_id][sid] >= 5:
                            track_to_student[track_id] = sid
                            
                            # Restore historical risk score if exists
                            hist_score = historical_risk_scores.get(sid, 0)
                            
                            if sid not in tracked_students:
                                tracked_students[sid] = {
                                    "name": best_match['name'], 
                                    "risk_score": hist_score, 
                                    "status": "Active", 
                                    "direction": "CENTER", 
                                    "last_seen": now, 
                                    "last_update": now
                                }

            # If identified, process head pose
            if track_id in track_to_student:
                sid = track_to_student[track_id]
                current_students_in_frame.add(sid)
                name = tracked_students[sid]["name"]
                
                # Yunet Head Direction on cropped body
                direction = "CENTER"
                detector.setInputSize((person_crop.shape[1], person_crop.shape[0]))
                _, faces = detector.detect(person_crop)
                
                if faces is not None and len(faces) > 0:
                    # Pick largest face
                    faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
                    face = faces[0]
                    x_face, y_face, w_face, h_face = map(int, face[:4])
                    x_nose, y_nose = map(int, face[8:10])
                    if w_face > 0 and h_face > 0:
                        nx = (x_nose - x_face) / w_face
                        ny = (y_nose - y_face) / h_face
                        
                        # Dynamic Overhead Calibration
                        if sid not in baseline_calibration:
                            baseline_calibration[sid] = {"nx": [], "ny": []}
                            
                        # Calibrate for the first 30 frames
                        if len(baseline_calibration[sid]["nx"]) < 30:
                            baseline_calibration[sid]["nx"].append(nx)
                            baseline_calibration[sid]["ny"].append(ny)
                            # Assume CENTER during calibration
                            direction = "CENTER"
                        else:
                            # Use baseline average
                            base_nx = sum(baseline_calibration[sid]["nx"]) / 30.0
                            base_ny = sum(baseline_calibration[sid]["ny"]) / 30.0
                            
                            # Dynamic Thresholds (adjust based on baseline)
                            if nx < base_nx - 0.15: direction = "RIGHT"
                            elif nx > base_nx + 0.15: direction = "LEFT"
                            elif ny < base_ny - 0.15: direction = "UP"
                            elif ny > base_ny + 0.15: direction = "DOWN"
                else:
                    direction = "OCCLUDED"
                
                # Head pose stabilization buffer
                if sid not in head_pose_buffers:
                    head_pose_buffers[sid] = []
                head_pose_buffers[sid].append(direction)
                if len(head_pose_buffers[sid]) > 3:
                    head_pose_buffers[sid].pop(0)
                
                # Only use direction if consistent across 3 frames, else use last known direction
                stable_direction = tracked_students[sid].get("direction", "CENTER")
                if len(head_pose_buffers[sid]) == 3 and all(d == head_pose_buffers[sid][0] for d in head_pose_buffers[sid]):
                    stable_direction = head_pose_buffers[sid][0]

                # Update Risk Score
                last_update = tracked_students[sid].get("last_update", now)
                delta_t = now - last_update
                tracked_students[sid]["last_update"] = now
                tracked_students[sid]["last_seen"] = now
                tracked_students[sid]["direction"] = stable_direction
                
                if SESSION_ACTIVE:
                    if phone_detected:
                        tracked_students[sid]["risk_score"] = min(100, tracked_students[sid]["risk_score"] + (20 * delta_t))
                        tracked_students[sid]["status"] = "Phone Detected"
                    elif book_detected:
                        tracked_students[sid]["risk_score"] = min(100, tracked_students[sid]["risk_score"] + (15 * delta_t))
                        tracked_students[sid]["status"] = "Book Detected"
                    elif stable_direction == "OCCLUDED":
                        tracked_students[sid]["risk_score"] = min(100, tracked_students[sid]["risk_score"] + (10 * delta_t))
                        tracked_students[sid]["status"] = "Face Occluded/Hidden"
                    elif stable_direction != "CENTER":
                        tracked_students[sid]["risk_score"] = min(100, tracked_students[sid]["risk_score"] + (5 * delta_t))
                        tracked_students[sid]["status"] = f"Looking {stable_direction}"
                    else:
                        tracked_students[sid]["status"] = "Looking Straight"
                        
                # Update historical cache
                historical_risk_scores[sid] = tracked_students[sid]["risk_score"]
                
                # Draw
                color = (0, 255, 0)
                if tracked_students[sid]["risk_score"] > 25:
                    color = (0, 165, 255) # Orange
                if tracked_students[sid]["risk_score"] > 75:
                    color = (0, 0, 255) # Red
                    
                    
                label = f"{name} ({sid}) Risk: {int(tracked_students[sid]['risk_score'])}"
                if not SESSION_ACTIVE:
                    label = f"{name} (Paused)"
                cv2.rectangle(frame, (tx1, ty1), (tx2, ty2), color, 2)
                cv2.putText(frame, label, (tx1, ty1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            else:
                unknown_count += 1
                cv2.rectangle(frame, (tx1, ty1), (tx2, ty2), (0, 0, 255), 2)
                cv2.putText(frame, f"UNKNOWN {track_id}", (tx1, ty1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        # Update offline students
        for sid in list(tracked_students.keys()):
            if sid not in current_students_in_frame:
                last_update = tracked_students[sid].get("last_update", now)
                delta_t = now - last_update
                tracked_students[sid]["last_update"] = now
                
                if SESSION_ACTIVE:
                    tracked_students[sid]["risk_score"] = min(100, tracked_students[sid]["risk_score"] + (2 * delta_t))
                tracked_students[sid]["status"] = "Away"
                
                time_away = now - tracked_students[sid].get("last_seen", 0)
                if time_away > 60.0:
                    historical_risk_scores[sid] = tracked_students[sid]["risk_score"]
                    if sid in baseline_calibration:
                        del baseline_calibration[sid]
                    del tracked_students[sid]
                    to_delete = [tid for tid, s in track_to_student.items() if s == sid]
                    for tid in to_delete:
                        del track_to_student[tid]
                        if tid in track_votes:
                            del track_votes[tid]

        room_state["unknown_count"] = unknown_count
        
        status = "NORMAL"
        if phone_detected:
            status = "PHONE DETECTED"
        elif book_detected:
            status = "BOOK DETECTED"
        elif unknown_count > 0:
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
        "phone_detected": room_state.get("phone_detected", False),
        "book_detected": room_state.get("book_detected", False),
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
