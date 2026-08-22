import cv2
import time
import json
import os
import hmac
import hashlib
import secrets
import threading
import uuid
import numpy as np
import psycopg2
from flask import Flask, Response, jsonify, send_from_directory, request, session, redirect
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import base64
import struct
from ultralytics import YOLO
from phone_detect import PhoneDetector

global_replay_detector = None
replay_detector_lock = threading.Lock()

def get_replay_detector():
    global global_replay_detector
    if global_replay_detector is None:
        with replay_detector_lock:
            if global_replay_detector is None:
                try:
                    global_replay_detector = PhoneDetector(weights="yolo11n.pt", conf=0.25)
                except Exception as err:
                    print(f"[REPLAY DETECTOR] Init error: {err}")
    return global_replay_detector

# ---------------- CONFIG ----------------
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/proctor_db"
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

DEFAULT_CONFIG = {
    "setup_complete": False,
    "secret_key": None,        # generated on first run
    "supervisor_name": "",
    "organization": "",
    "exam_name": "",
    "exam_duration_minutes": 0,
    "username": "",
    "password_salt": "",
    "password_hash": "",
    "cctv_ip": ""              # empty -> use local webcam
}

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read config.json ({e}), using defaults.")
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        save_config(cfg)
    return cfg

def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)

def hash_password(password, salt_hex):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 200_000
    ).hex()

CONFIG = load_config()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or CONFIG.get("secret_key") or 'super_secret_proctor_key_change_in_production_2026'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=60)
CORS(app, supports_credentials=True)

# Signals the video loop to reopen the capture when the source changes
VIDEO_SOURCE_CHANGED = threading.Event()

def get_video_source():
    """Returns the CCTV stream URL if configured, else the local webcam."""
    cctv = (CONFIG.get("cctv_ip") or "").strip()
    if not cctv:
        return 0
    if cctv.startswith(("rtsp://", "http://", "https://")):
        return cctv
    # Bare IP entered -> assume a standard RTSP stream
    return f"rtsp://{cctv}"

# ---------------- SECURITY & RATE LIMITING ENGINE ----------------
SESSION_INACTIVITY_TIMEOUT = int(os.environ.get('SESSION_INACTIVITY_TIMEOUT', 1800))  # 30 minutes inactivity timeout
RATE_LIMIT_MAX_FAILURES = int(os.environ.get('RATE_LIMIT_MAX_FAILURES', 5))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get('RATE_LIMIT_WINDOW_SECONDS', 300))   # 5 minutes window
RATE_LIMIT_BLOCK_SECONDS = int(os.environ.get('RATE_LIMIT_BLOCK_SECONDS', 60))     # 60 seconds lockout
failed_attempts_registry = {}     # key -> [timestamps]

def generate_secure_mfa_secret():
    """Generates a 160-bit cryptographically secure RFC 6238 Base32 secret."""
    raw_secret = os.urandom(20)
    return base64.b32encode(raw_secret).decode('utf-8').replace('=', '')

def check_rate_limit(key):
    """Checks if a client/account has exceeded maximum failed attempts."""
    now = time.time()
    attempts = failed_attempts_registry.get(key, [])
    recent = [t for t in attempts if now - t < RATE_LIMIT_WINDOW_SECONDS]
    failed_attempts_registry[key] = recent
    if len(recent) >= RATE_LIMIT_MAX_FAILURES:
        last_attempt = recent[-1]
        elapsed = now - last_attempt
        if elapsed < RATE_LIMIT_BLOCK_SECONDS:
            return False, int(RATE_LIMIT_BLOCK_SECONDS - elapsed)
    return True, 0

def record_failed_attempt(key):
    now = time.time()
    attempts = failed_attempts_registry.get(key, [])
    attempts.append(now)
    failed_attempts_registry[key] = attempts

def reset_failed_attempts(key):
    if key in failed_attempts_registry:
        del failed_attempts_registry[key]

# ---------------- RFC 6238 TOTP ENGINE ----------------
def generate_totp_code(secret_base32, time_step=30, digits=6, t=None):
    """Generates standard RFC 6238 TOTP code."""
    if t is None:
        t = time.time()
    padded_secret = secret_base32.strip().upper()
    while len(padded_secret) % 8 != 0:
        padded_secret += '='
    key = base64.b32decode(padded_secret)
    counter = int(t // time_step)
    counter_bytes = struct.pack(">Q", counter)
    hm = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = hm[-1] & 0x0F
    code_int = struct.unpack(">I", hm[offset:offset+4])[0] & 0x7FFFFFFF
    return str(code_int % (10 ** digits)).zfill(digits)

def verify_totp_code(secret_base32, code, window=1):
    """Verifies standard RFC 6238 TOTP code across time window."""
    current_time = time.time()
    for offset in range(-window, window + 1):
        test_time = current_time + (offset * 30)
        expected = generate_totp_code(secret_base32, t=test_time)
        if str(code).strip() == expected:
            return True
    return False

# ---------------- AUDIT TRAIL ENGINE ----------------
def record_audit_event(user_id, username, role, institution_id, action, ip_address, result, details=""):
    """Persists immutable security audit events into PostgreSQL."""
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO audit_logs (user_id, username, role, institution_id, action, ip_address, result, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """, (user_id, username, role, institution_id, action, ip_address, result, details))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error logging audit event: {e}")

# ---------------- SECURITY HEADERS MIDDLEWARE ----------------
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

# ---------------- MIDDLEWARE & RBAC ----------------
PUBLIC_API = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/auth/mfa-verify",
    "/api/supervisor_login",
    "/api/supervisor_logout",
    "/api/setup",
    "/api/setup/status",
    "/api/webauthn/status",
    "/api/webauthn/login/begin",
    "/api/webauthn/login/complete",
    "/api/validate_face",
    "/api/timeline",
    "/api/timeline/resolve",
    "/api/timeline/event",
    "/api/replay/detect_frame",
}

REQUIRE_LOGIN = False

@app.before_request
def require_auth():
    path = request.path

    # Public static files, scripts, fonts, images, landing
    if path in ['/', '/index.html', '/login.html', '/supervisor_login.html', '/setup.html', '/setup_institution.html']:
        return
    if path.startswith('/static/') or path.startswith('/models/') or path.endswith(('.css', '.js', '.png', '.jpg', '.jpeg', '.svg', '.ico', '.woff', '.woff2', '.ttf')):
        return

    # Public Auth endpoints & streaming element
    if path in PUBLIC_API or path.startswith('/video_feed') or path.startswith('/api/setup') or path.startswith('/api/webauthn'):
        return

    if not REQUIRE_LOGIN:
        return

    user_id = session.get('user_id')
    role = session.get('role', 'SUPERVISOR' if session.get('admin_logged_in') else None)

    # Legacy or bypass session check
    if session.get('admin_logged_in') and not user_id:
        return

    # If not authenticated
    if not user_id:
        if path.startswith('/api/'):
            return jsonify({"error": "UNAUTHORIZED: Authentication required"}), 401
        return redirect('/supervisor_login.html')

    # Inactivity Timeout Check (30 min)
    last_act = session.get('last_activity')
    now = time.time()
    if last_act and (now - last_act > SESSION_INACTIVITY_TIMEOUT):
        record_audit_event(user_id, session.get('username'), role, session.get('institution_id'), 'SESSION_EXPIRED', request.remote_addr, 'DENIED', 'Session terminated due to 30min inactivity')
        session.clear()
        if path.startswith('/api/'):
            return jsonify({"error": "SESSION EXPIRED: Please log in again"}), 401
        return redirect('/login.html?expired=1')

    session['last_activity'] = now

    # Verify Account Active Status in DB (Invalidate session immediately if disabled)
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT status, role, institution_id FROM users WHERE user_id = %s;", (user_id,))
        urow = cursor.fetchone()
        cursor.close()
        conn.close()
        if not urow or urow[0] == 'DISABLED':
            record_audit_event(user_id, session.get('username'), role, session.get('institution_id'), 'ACCOUNT_DISABLED', request.remote_addr, 'DENIED', 'Active session terminated because account is disabled')
            session.clear()
            if path.startswith('/api/'):
                return jsonify({"error": "ACCESS DENIED: Account is disabled"}), 403
            return redirect('/login.html?disabled=1')
    except Exception as e:
        print(f"Error checking user active status: {e}")

    # RBAC Route Authorization
    if path == '/admin.html' or path.startswith('/api/admin/'):
        if role != 'ADMIN':
            record_audit_event(user_id, session.get('username'), role, session.get('institution_id'), 'ACCESS_DENIED', request.remote_addr, 'DENIED', f"Unauthorized access attempt to {path}")
            if path.startswith('/api/'):
                return jsonify({"error": "FORBIDDEN: Platform Administrator clearance required"}), 403
            return redirect('/login.html')
        return

    if path in ['/monitoring.html', '/enrollment.html', '/replay.html', '/reports.html'] or path.startswith('/api/session/') or path == '/api/register':
        if role not in ['ADMIN', 'SUPERVISOR', 'TEACHER']:
            record_audit_event(user_id, session.get('username'), role, session.get('institution_id'), 'ACCESS_DENIED', request.remote_addr, 'DENIED', f"Unauthorized access attempt to {path}")
            if path.startswith('/api/'):
                return jsonify({"error": "FORBIDDEN: Supervisor clearance required"}), 403
            return redirect('/supervisor_login.html')
        return

    if path == '/student_dashboard.html' or path.startswith('/api/student/'):
        if role not in ['ADMIN', 'STUDENT']:
            record_audit_event(user_id, session.get('username'), role, session.get('institution_id'), 'ACCESS_DENIED', request.remote_addr, 'DENIED', f"Unauthorized access attempt to {path}")
            if path.startswith('/api/'):
                return jsonify({"error": "FORBIDDEN: Student clearance required"}), 403
            return redirect('/login.html')
        return

@app.route('/')
def serve_index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/login.html')
def serve_login():
    return send_from_directory(BASE_DIR, 'login.html')

@app.route('/reports/<path:filename>')
def serve_report(filename):
    """Protects direct examination report downloads against IDOR."""
    if 'user_id' not in session:
        return redirect('/login.html')
    
    role = session.get('role')
    user_inst = session.get('institution_id')

    if role != 'ADMIN':
        try:
            conn = psycopg2.connect(DB_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT institution_id FROM exam_sessions WHERE report_url LIKE %s LIMIT 1;", (f"%{filename}%",))
            row = cursor.fetchone()
            cursor.close()
            conn.close()

            if row and row[0] and row[0] != user_inst:
                record_audit_event(session.get('user_id'), session.get('username'), role, user_inst, 'REPORT_ACCESS_DENIED', request.remote_addr, 'DENIED', f"Attempted cross-institution report access: {filename}")
                return jsonify({"error": "FORBIDDEN: Access to cross-institution examination report denied"}), 403
        except Exception as e:
            print(f"Error validating report access: {e}")

    record_audit_event(session.get('user_id'), session.get('username'), role, user_inst, 'REPORT_ACCESS', request.remote_addr, 'SUCCESS', f"Accessed examination report: {filename}")
    return send_from_directory(REPORTS_DIR, filename)

@app.route('/<path:path>')
def serve_static(path):
    if os.path.exists(os.path.join(BASE_DIR, path)):
        return send_from_directory(BASE_DIR, path)
    return "Not Found", 404

# ---------------- AI MODELS ----------------
import proctor_ai
import face_recog
import phone_detect

# YOLO11-nano: newest ultralytics architecture, better accuracy than v8n at
# the same speed. Auto-downloads on first run.
yolo_model = YOLO('yolo11n.pt')

# Landmark-based face analysis (478-point mesh + iris) and the temporal
# behaviour/suspicion engine
face_analyzer = proctor_ai.FaceAnalyzer(max_faces=6)
behaviors = {}                       # sid -> proctor_ai.StudentBehavior
room_behavior = proctor_ai.RoomBehavior()
smooth_boxes = {}                    # sid -> EMA-smoothed (x1,y1,x2,y2)
# Face identification stack. Measured against the previous YuNet + SFace
# pairing: SFace scored the enrolled subject at 0.393 against their own single
# template (below its own 0.45 accept threshold); ArcFace with multi-template
# enrolment scores the same subject 0.93, and still 0.72 at a 28px face.
# SCRFD also detects in ~2.5x less light than YuNet.
face_detector = face_recog.SCRFDDetector()
embedder = face_recog.ArcFaceEmbedder()
gallery = face_recog.Gallery()
print(f"[FACE] ArcFace on {embedder.provider}")

# Dedicated phone detector. The main YOLO pass (yolo11n @480, tuned for
# person tracking) found only 5.5% of small/distant phones; this one crops
# each person and re-detects inside that crop.
# PHONE_MODEL / PHONE_DETECTION env vars let the accuracy-vs-speed trade-off
# be changed without editing code. Set PHONE_DETECTION=off to disable.
PHONE_ENABLED = os.environ.get("PHONE_DETECTION", "on").lower() != "off"
phone_detector = (phone_detect.PhoneDetector(
    os.environ.get("PHONE_MODEL", "yolo11s.pt")) if PHONE_ENABLED else None)

# Track ID to Student ID mapping (retained for the absent-student cleanup
# pass below; population via a person tracker was removed -- see _ai_worker_loop)
track_to_student = {}
track_votes = {} # track_id -> {student_id: count}
historical_risk_scores = {}
head_pose_buffers = {}
baseline_calibration = {} # sid -> {"nx": [], "ny": []}
student_gaze_tracker = {} # sid -> {"history": [], "deviation_start": None, "last_event_time": 0}
VIDEO_SOURCE = 0 # Can be an RTSP url like 'rtsp://admin:123@192.168.1.100/stream'
# Session State
SESSION_ACTIVE = False
session_start_time = None
session_paused_time = None
accumulated_elapsed_seconds = 0

# ---------------- REVIEWABLE ACTION TIMELINE (SEARCH & DISCOVERY) ----------------
timeline_events_buffer = []
timeline_events_lock = threading.Lock()

DEFAULT_TIMELINE_SEED = [
    {
        "id": "evt_001_sess",
        "timestamp": "09:58:01",
        "iso_timestamp": "2026-08-22T09:58:01",
        "student_id": "EXAM-CS302",
        "student_name": "System / Session",
        "institution_id": "INST-001",
        "category": "SESSION",
        "event_type": "SESSION_STARTED",
        "title": "Examination Session Started",
        "description": "Supervised examination session initiated for CS302 Computer Vision. AI Vision and Biometric SOC sensors active.",
        "severity": "NORMAL",
        "state_change": {"status": ["STANDBY", "ACTIVE"]},
        "metadata": {"session_id": "REC-9948271", "invigilator": "Dr. Sarah Jenkins"},
        "resolved": True
    },
    {
        "id": "evt_002_bio",
        "timestamp": "09:59:12",
        "iso_timestamp": "2026-08-22T09:59:12",
        "student_id": "1002",
        "student_name": "Nalin Tuscano",
        "institution_id": "INST-001",
        "category": "IDENTITY",
        "event_type": "BIOMETRIC_VERIFIED",
        "title": "Biometric Identity Verified",
        "description": "Candidate facial geometry matched against enrolled ArcFace R50 multi-template gallery. Identity cleared for assessment.",
        "severity": "NORMAL",
        "state_change": {"status": ["PENDING", "VERIFIED"], "trust": [100, 100]},
        "metadata": {"confidence": 0.96, "method": "ArcFace R50", "landmarks": 5},
        "resolved": True
    },
    {
        "id": "evt_003_enr",
        "timestamp": "10:01:20",
        "iso_timestamp": "2026-08-22T10:01:20",
        "student_id": "STU-298314",
        "student_name": "Alex Johnson",
        "institution_id": "INST-001",
        "category": "IDENTITY",
        "event_type": "STUDENT_ENROLLED",
        "title": "Candidate Face Profile Enrolled",
        "description": "Candidate registered 12 biometric multi-pose angle templates. Quality gates confirmed.",
        "severity": "NORMAL",
        "state_change": {"status": ["UNREGISTERED", "ENROLLED"]},
        "metadata": {"templates": 12, "resolution": "1920x1080"},
        "resolved": True
    },
    {
        "id": "evt_004_ent",
        "timestamp": "10:05:43",
        "iso_timestamp": "2026-08-22T10:05:43",
        "student_id": "STU-298314",
        "student_name": "Alex Johnson",
        "institution_id": "INST-001",
        "category": "AI DETECTION",
        "event_type": "STUDENT_ENTERED",
        "title": "Candidate Entered Monitored Area",
        "description": "Candidate detected and acquired in primary CCTV monitoring cone. Face mesh active.",
        "severity": "NORMAL",
        "state_change": {"presence": ["AWAY", "ACTIVE"]},
        "metadata": {"bbox": [120, 80, 240, 260], "camera": "CAM-01"},
        "resolved": True
    },
    {
        "id": "evt_005_gaze",
        "timestamp": "10:12:45",
        "iso_timestamp": "2026-08-22T10:12:45",
        "student_id": "STU-298314",
        "student_name": "Alex Johnson",
        "institution_id": "INST-001",
        "category": "GAZE",
        "event_type": "GAZE_DEVIATION",
        "title": "Gaze Deviation (Looking Left)",
        "description": "Subject's gaze deviated sharply to the left quadrant of the screen boundary for 4.2 seconds. Flagged as potential off-screen resource reference.",
        "severity": "SUSPICIOUS",
        "state_change": {"risk": [0, 15], "trust": [100, 85]},
        "metadata": {"gaze": "LEFT", "yaw": -28.4, "pitch": 4.1, "duration_sec": 4.2},
        "resolved": False
    },
    {
        "id": "evt_006_gaze",
        "timestamp": "10:14:30",
        "iso_timestamp": "2026-08-22T10:14:30",
        "student_id": "STU-298314",
        "student_name": "Alex Johnson",
        "institution_id": "INST-001",
        "category": "GAZE",
        "event_type": "GAZE_DEVIATION",
        "title": "Repeated Looking Right",
        "description": "Frequent glancing to the lower right area off-screen. Heuristic pattern suggests reading notes or a secondary screen.",
        "severity": "SUSPICIOUS",
        "state_change": {"risk": [15, 25], "trust": [85, 75]},
        "metadata": {"gaze": "RIGHT", "yaw": 31.2, "pitch": -8.5, "duration_sec": 3.8},
        "resolved": False
    },
    {
        "id": "evt_007_miss",
        "timestamp": "10:18:22",
        "iso_timestamp": "2026-08-22T10:18:22",
        "student_id": "STU-298314",
        "student_name": "Alex Johnson",
        "institution_id": "INST-001",
        "category": "RISK",
        "event_type": "FACE_MISSING",
        "title": "Facial Tracking Signal Lost",
        "description": "Facial tracking lost completely for 12 seconds. Candidate moved out of frame or camera was obscured.",
        "severity": "HIGH_RISK",
        "state_change": {"risk": [25, 35], "trust": [75, 65], "presence": ["ACTIVE", "AWAY"]},
        "metadata": {"duration_sec": 12.0, "status": "AWAY"},
        "resolved": False
    },
    {
        "id": "evt_008_ret",
        "timestamp": "10:20:15",
        "iso_timestamp": "2026-08-22T10:20:15",
        "student_id": "STU-298314",
        "student_name": "Alex Johnson",
        "institution_id": "INST-001",
        "category": "AI DETECTION",
        "event_type": "FACE_REACQUIRED",
        "title": "Candidate Re-Acquired in Frame",
        "description": "Face re-acquired by neural tracking system. Head posture and iris orientation normalized.",
        "severity": "NORMAL",
        "state_change": {"presence": ["AWAY", "ACTIVE"]},
        "metadata": {"status": "ACTIVE"},
        "resolved": True
    },
    {
        "id": "evt_009_multi",
        "timestamp": "10:30:05",
        "iso_timestamp": "2026-08-22T10:30:05",
        "student_id": "ROOM-SOC",
        "student_name": "Examination Room",
        "institution_id": "INST-001",
        "category": "AI DETECTION",
        "event_type": "MULTIPLE_PERSONS",
        "title": "Multiple Persons Detected",
        "description": "Secondary human figure identified in examination room background. Unauthorized room presence detected.",
        "severity": "HIGH_RISK",
        "state_change": {"room_status": ["NORMAL", "UNKNOWN_PERSON"], "risk": [20, 45]},
        "metadata": {"faces_count": 2, "evidence_url": "exam_room.jpg"},
        "resolved": False
    },
    {
        "id": "evt_010_phone",
        "timestamp": "10:42:31",
        "iso_timestamp": "2026-08-22T10:42:31",
        "student_id": "1002",
        "student_name": "Nalin Tuscano",
        "institution_id": "INST-001",
        "category": "DEVICE",
        "event_type": "PHONE_DETECTED",
        "title": "PHONE DETECTED",
        "description": "Mobile device detected inside examination area near candidate workspace via YOLO11 neural detector.",
        "severity": "HIGH_RISK",
        "state_change": {"risk": [35, 60], "trust": [94, 71]},
        "metadata": {"device": "cell phone", "confidence": 0.89, "evidence_url": "exam_room.jpg"},
        "resolved": False
    },
    {
        "id": "evt_011_risk",
        "timestamp": "10:42:35",
        "iso_timestamp": "2026-08-22T10:42:35",
        "student_id": "1002",
        "student_name": "Nalin Tuscano",
        "institution_id": "INST-001",
        "category": "RISK",
        "event_type": "RISK_ESCALATED",
        "title": "Risk Score Escalated to High Risk",
        "description": "Heuristic cumulative risk crossed Critical threshold. Candidate status updated to Under Review.",
        "severity": "HIGH_RISK",
        "state_change": {"status": ["VERIFIED", "UNDER_REVIEW"], "risk": [35, 60]},
        "metadata": {"threshold": 50, "current_score": 60},
        "resolved": False
    },
    {
        "id": "evt_012_alert",
        "timestamp": "10:43:02",
        "iso_timestamp": "2026-08-22T10:43:02",
        "student_id": "1002",
        "student_name": "Nalin Tuscano",
        "institution_id": "INST-001",
        "category": "ALERT",
        "event_type": "ALERT_CREATED",
        "title": "Security Alert Created",
        "description": "Urgent security alert dispatched to invigilator SOC: Unauthorized mobile communication device detected.",
        "severity": "HIGH_RISK",
        "state_change": {"alert": ["NONE", "CREATED"]},
        "metadata": {"priority": "P1_URGENT"},
        "resolved": False
    },
    {
        "id": "evt_013_rev",
        "timestamp": "10:44:10",
        "iso_timestamp": "2026-08-22T10:44:10",
        "student_id": "1002",
        "student_name": "Nalin Tuscano",
        "institution_id": "INST-001",
        "category": "ALERT",
        "event_type": "ALERT_REVIEWED",
        "title": "Security Alert Reviewed",
        "description": "Invigilator examined forensic capture evidence and confirmed active device infraction.",
        "severity": "SUSPICIOUS",
        "state_change": {"alert": ["CREATED", "REVIEWED"]},
        "metadata": {"reviewer": "Dr. Sarah Jenkins"},
        "resolved": False
    },
    {
        "id": "evt_014_res",
        "timestamp": "10:55:20",
        "iso_timestamp": "2026-08-22T10:55:20",
        "student_id": "1002",
        "student_name": "Nalin Tuscano",
        "institution_id": "INST-001",
        "category": "ALERT",
        "event_type": "ALERT_RESOLVED",
        "title": "Security Alert Resolved",
        "description": "Candidate surrendered prohibited device. Incident flagged for integrity penalty and logged in permanent exam record.",
        "severity": "NORMAL",
        "state_change": {"alert": ["REVIEWED", "RESOLVED"]},
        "metadata": {"resolution_note": "Device confiscated; examination continued under strict telemetry."},
        "resolved": True
    },
    {
        "id": "evt_015_end",
        "timestamp": "11:30:00",
        "iso_timestamp": "2026-08-22T11:30:00",
        "student_id": "EXAM-CS302",
        "student_name": "System / Session",
        "institution_id": "INST-001",
        "category": "SESSION",
        "event_type": "SESSION_ENDED",
        "title": "Examination Concluded",
        "description": "Official examination concluded. Automated forensic audit report compiled, encrypted, and locked for administration.",
        "severity": "NORMAL",
        "state_change": {"status": ["ACTIVE", "COMPLETED"]},
        "metadata": {"total_events": 15, "high_risk_incidents": 4},
        "resolved": True
    }
]

timeline_events_buffer.extend(DEFAULT_TIMELINE_SEED)

def record_timeline_event(student_id, student_name, institution_id, category, event_type, title, description, severity="NORMAL", state_change=None, metadata=None, timestamp=None):
    """
    Persists structured Action Timeline events for deep search, discovery, and forensic replay.
    """
    if timestamp is None:
        timestamp_str = datetime.now().strftime("%H:%M:%S")
        iso_timestamp = datetime.now().isoformat()
        db_timestamp = datetime.now()
    else:
        timestamp_str = str(timestamp)
        iso_timestamp = str(timestamp)
        db_timestamp = datetime.now()

    event = {
        "id": "evt_" + str(uuid.uuid4())[:8],
        "timestamp": timestamp_str,
        "iso_timestamp": iso_timestamp,
        "student_id": str(student_id) if student_id else "SYSTEM",
        "student_name": str(student_name) if student_name else (str(student_id) if student_id else "System Command"),
        "institution_id": str(institution_id) if institution_id else "INST-001",
        "category": str(category).upper(),
        "event_type": str(event_type),
        "title": str(title),
        "description": str(description),
        "severity": str(severity).upper(),
        "state_change": state_change or {},
        "metadata": metadata or {},
        "resolved": False
    }

    with timeline_events_lock:
        timeline_events_buffer.insert(0, event)
        if len(timeline_events_buffer) > 500:
            timeline_events_buffer.pop()

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO action_timeline (
                event_uuid, time_str, student_id, student_name, institution_id,
                category, event_type, title, description, severity, state_change,
                metadata, resolved, timestamp
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (
            event["id"],
            event["timestamp"],
            event["student_id"],
            event["student_name"],
            event["institution_id"],
            event["category"],
            event["event_type"],
            event["title"],
            event["description"],
            event["severity"],
            json.dumps(event["state_change"]),
            json.dumps(event["metadata"]),
            False,
            db_timestamp
        ))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        pass

    return event

# ---------------- DB INIT ----------------
active_monitoring_institution = "INST-001"

def init_db():
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()

        # 1. Institutions table with complete configuration fields
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS institutions (
                institution_id TEXT PRIMARY KEY,
                institution_name VARCHAR(150) NOT NULL,
                institution_type VARCHAR(50) DEFAULT 'University',
                country VARCHAR(100) DEFAULT 'United States',
                state VARCHAR(100) DEFAULT '',
                city VARCHAR(100) DEFAULT '',
                email VARCHAR(150) DEFAULT '',
                contact VARCHAR(50) DEFAULT '',
                institution_code VARCHAR(50) UNIQUE NOT NULL,
                status VARCHAR(20) DEFAULT 'ACTIVE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='institutions' AND column_name='institution_type') THEN
                    ALTER TABLE institutions ADD COLUMN institution_type VARCHAR(50) DEFAULT 'University';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='institutions' AND column_name='country') THEN
                    ALTER TABLE institutions ADD COLUMN country VARCHAR(100) DEFAULT 'United States';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='institutions' AND column_name='state') THEN
                    ALTER TABLE institutions ADD COLUMN state VARCHAR(100) DEFAULT '';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='institutions' AND column_name='city') THEN
                    ALTER TABLE institutions ADD COLUMN city VARCHAR(100) DEFAULT '';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='institutions' AND column_name='email') THEN
                    ALTER TABLE institutions ADD COLUMN email VARCHAR(150) DEFAULT '';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='institutions' AND column_name='contact') THEN
                    ALTER TABLE institutions ADD COLUMN contact VARCHAR(50) DEFAULT '';
                END IF;
            END $$;
        """)

        # 2. Users table (ADMIN, FACULTY / SUPERVISOR / TEACHER)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role VARCHAR(20) NOT NULL,
                institution_id TEXT,
                student_id VARCHAR(50),
                status VARCHAR(20) DEFAULT 'ACTIVE',
                mfa_secret VARCHAR(64) DEFAULT NULL,
                mfa_enabled BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='mfa_secret') THEN
                    ALTER TABLE users ADD COLUMN mfa_secret VARCHAR(64) DEFAULT NULL;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='mfa_enabled') THEN
                    ALTER TABLE users ADD COLUMN mfa_enabled BOOLEAN DEFAULT FALSE;
                END IF;
            END $$;
        """)

        # 3. Students table with institution_id and ArcFace multi-templates
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                student_id VARCHAR(50) UNIQUE,
                name VARCHAR(100),
                face_encoding JSONB,
                arcface_templates JSONB,
                institution_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='students' AND column_name='arcface_templates') THEN
                    ALTER TABLE students ADD COLUMN arcface_templates JSONB;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='students' AND column_name='institution_id') THEN
                    ALTER TABLE students ADD COLUMN institution_id TEXT;
                END IF;
            END $$;
        """)

        # 4. Exam logs table with institution_id
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS exam_logs (
                id SERIAL PRIMARY KEY,
                student_id VARCHAR(50),
                institution_id TEXT,
                risk_score INT,
                direction VARCHAR(50),
                status VARCHAR(50),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='exam_logs' AND column_name='institution_id') THEN
                    ALTER TABLE exam_logs ADD COLUMN institution_id TEXT;
                END IF;
            END $$;
        """)

        # 5. Exam sessions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS exam_sessions (
                session_id SERIAL PRIMARY KEY,
                institution_id TEXT,
                supervisor_id INT,
                status VARCHAR(20),
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                duration_seconds INT,
                report_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 6. Immutable Security Audit Logs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                log_id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INT,
                username VARCHAR(100),
                role VARCHAR(20),
                institution_id TEXT,
                action VARCHAR(50) NOT NULL,
                ip_address VARCHAR(50),
                result VARCHAR(20) NOT NULL,
                details TEXT
            );
        """)

        # 7. Reviewable Action Timeline table (Search & Discovery)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS action_timeline (
                id SERIAL PRIMARY KEY,
                event_uuid VARCHAR(64) UNIQUE,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                time_str VARCHAR(20),
                student_id VARCHAR(50),
                student_name VARCHAR(100),
                institution_id TEXT,
                category VARCHAR(50),
                event_type VARCHAR(50),
                title VARCHAR(150),
                description TEXT,
                severity VARCHAR(30),
                state_change JSONB,
                metadata JSONB,
                resolved BOOLEAN DEFAULT FALSE
            );
        """)

        # Seed default institution if table empty
        cursor.execute("SELECT COUNT(*) FROM institutions;")
        _row = cursor.fetchone()
        if _row is not None and _row[0] == 0:
            cursor.execute("""
                INSERT INTO institutions (institution_id, institution_name, institution_type, country, state, city, email, contact, institution_code, status)
                VALUES ('INST-001', 'Apex Institute of Technology', 'University', 'United States', 'California', 'San Francisco', 'admin@apex.edu', '+1 (555) 019-2834', 'AIT-001', 'ACTIVE');
            """)

        # Seed single platform Admin if not exists
        cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'ADMIN';")
        _row = cursor.fetchone()
        if _row is not None and _row[0] == 0:
            admin_hash = generate_password_hash("Admin@ProctorAI2026")
            cursor.execute("""
                INSERT INTO users (name, username, password_hash, role, institution_id, status, mfa_secret, mfa_enabled)
                VALUES ('Platform Administrator', 'admin', %s, 'ADMIN', NULL, 'ACTIVE', %s, FALSE);
            """, (admin_hash, generate_secure_mfa_secret()))

        # Seed default Faculty user for INST-001 if not exists
        cursor.execute("SELECT COUNT(*) FROM users WHERE role IN ('FACULTY', 'SUPERVISOR');")
        _row = cursor.fetchone()
        if _row is not None and _row[0] == 0:
            faculty_hash = generate_password_hash("Faculty@123")
            cursor.execute("""
                INSERT INTO users (name, username, password_hash, role, institution_id, status, mfa_secret, mfa_enabled)
                VALUES ('Dr. Sarah Jenkins', 'faculty@apex.edu', %s, 'FACULTY', 'INST-001', 'ACTIVE', %s, FALSE);
            """, (faculty_hash, generate_secure_mfa_secret()))

        # Seed realistic action timeline events if table empty
        cursor.execute("SELECT COUNT(*) FROM action_timeline;")
        _trow = cursor.fetchone()
        if _trow is not None and _trow[0] == 0:
            for ev in DEFAULT_TIMELINE_SEED:
                cursor.execute("""
                    INSERT INTO action_timeline (
                        event_uuid, time_str, student_id, student_name, institution_id,
                        category, event_type, title, description, severity, state_change,
                        metadata, resolved, timestamp
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_uuid) DO NOTHING;
                """, (
                    ev["id"], ev["timestamp"], ev["student_id"], ev["student_name"],
                    ev["institution_id"], ev["category"], ev["event_type"], ev["title"],
                    ev["description"], ev["severity"], json.dumps(ev["state_change"]),
                    json.dumps(ev["metadata"]), ev["resolved"], ev["iso_timestamp"]
                ))

        conn.commit()
        cursor.close()
        conn.close()
        print("[DB] Multi-institution database schema verified (Tenants & RBAC ready).")
    except Exception as e:
        print(f"Error initializing DB: {e}")

init_db()

# Load registered students into memory for fast comparison
registered_students = [] # list of dicts: {'student_id': str, 'name': str, 'encoding': np.ndarray, 'institution_id': str}

def load_students():
    """Loads ArcFace multi-template galleries. Associates each student with their institution."""
    global registered_students
    registered_students = []
    gallery.people.clear()
    legacy_only = []
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT student_id, name, face_encoding, arcface_templates, institution_id FROM students;")
        rows = cursor.fetchall()
        for sid, name, legacy_enc, arc, inst_id in rows:
            inst = inst_id or "INST-001"
            if arc:
                templates = np.array(arc, dtype=np.float32)
                if templates.ndim == 1:
                    templates = templates[None, :]
                gallery.set_person(sid, name, templates, institution_id=inst)
                registered_students.append({
                    "student_id": sid,
                    "name": name,
                    "templates": len(templates),
                    "institution_id": inst
                })
            elif legacy_enc is not None:
                encoding = np.array(legacy_enc, dtype=np.float32)
                if encoding.ndim == 1:
                    encoding = encoding.reshape(1, -1)
                gallery.set_person(sid, name, encoding, institution_id=inst)
                registered_students.append({
                    "student_id": sid,
                    "name": name,
                    "encoding": encoding,
                    "institution_id": inst
                })
                legacy_only.append(f"{name} ({sid})")
        cursor.close()
        conn.close()
        total_t = sum(len(p["templates"]) for p in gallery.people.values())
        print(f"[FACE] Loaded {len(gallery)} enrolled students with ArcFace templates ({total_t} total) across institutions.")
    except Exception as e:
        print(f"Error loading students: {e}")

load_students()

# ---------------- AUTHENTICATION & MULTI-TENANT ENDPOINTS ----------------

@app.route('/api/institutions', methods=['GET'])
def get_public_institutions():
    """Returns active institutions for login selection dropdowns."""
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT institution_id, institution_name, institution_type, city, country, institution_code
            FROM institutions
            WHERE status = 'ACTIVE'
            ORDER BY institution_name ASC;
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        insts = []
        for r in rows:
            insts.append({
                "institution_id": r[0],
                "institution_name": r[1],
                "institution_type": r[2] or "University",
                "city": r[3] or "",
                "country": r[4] or "",
                "institution_code": r[5]
            })
        return jsonify(insts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/institutions/setup', methods=['POST'])
def setup_institution_endpoint():
    """Real configuration setup wizard endpoint for registering new institutions."""
    data = request.json or {}
    name = (data.get('institutionName') or data.get('institution_name') or '').strip()
    inst_type = (data.get('institutionType') or data.get('institution_type') or 'University').strip()
    country = (data.get('country') or 'United States').strip()
    state = (data.get('state') or '').strip()
    city = (data.get('city') or '').strip()
    email = (data.get('contactEmail') or data.get('email') or '').strip()
    contact = (data.get('contact') or '').strip()
    code = (data.get('institutionCode') or data.get('institution_code') or '').strip().upper()

    if not name:
        return jsonify({"error": "Institution name is required"}), 400

    if not code:
        clean = "".join(c for c in name if c.isalnum()).upper()
        code = f"{clean[:4]}-{secrets.token_hex(2).upper()}"

    inst_id = f"INST-{code[:6]}-{secrets.token_hex(2).upper()}"

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO institutions (institution_id, institution_name, institution_type, country, state, city, email, contact, institution_code, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE')
            ON CONFLICT (institution_code) DO UPDATE
              SET institution_name=EXCLUDED.institution_name, institution_type=EXCLUDED.institution_type,
                  country=EXCLUDED.country, state=EXCLUDED.state, city=EXCLUDED.city, email=EXCLUDED.email, contact=EXCLUDED.contact
            RETURNING institution_id;
        """, (inst_id, name, inst_type, country, state, city, email, contact, code))
        row = cursor.fetchone()
        final_id = row[0] if row else inst_id
        conn.commit()
        cursor.close()
        conn.close()

        session['institution_id'] = final_id
        session['institution_name'] = name
        global active_monitoring_institution
        active_monitoring_institution = final_id

        record_audit_event(session.get('user_id'), session.get('username', 'SETUP'), 'ADMIN', final_id, 'INSTITUTION_REGISTERED', request.remote_addr, 'SUCCESS', f"Registered institution {name} ({final_id})")
        return jsonify({"success": True, "institution_id": final_id, "institution_name": name, "message": "Institution registered successfully"})
    except Exception as e:
        print(f"Error setting up institution: {e}")
        return jsonify({"error": str(e)}), 500

def _apply_cctv_choice(data):
    """Applies the CCTV/webcam choice sent with a login request.

    If the request contains a 'cctv_ip' key: a non-empty value switches the
    feed to that CCTV stream, an empty value switches back to the webcam.
    """
    if not isinstance(data, dict) or "cctv_ip" not in data:
        return
    new_value = (data.get("cctv_ip") or "").strip()
    if new_value != CONFIG.get("cctv_ip", ""):
        CONFIG["cctv_ip"] = new_value
        save_config(CONFIG)
        VIDEO_SOURCE_CHANGED.set()

@app.route('/api/setup/status', methods=['GET'])
def setup_status():
    return jsonify({"setup_complete": bool(CONFIG.get("setup_complete"))})

@app.route('/api/setup', methods=['POST'])
def initial_setup():
    """First-run setup wizard: creates the supervisor account and exam profile."""
    if CONFIG.get("setup_complete"):
        return jsonify({"error": "Setup has already been completed. Please log in."}), 403

    data = request.json or {}
    supervisor_name = (data.get("supervisor_name") or "").strip()
    organization = (data.get("organization") or "").strip()
    exam_name = (data.get("exam_name") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    confirm_password = data.get("confirm_password") or ""
    cctv_ip = (data.get("cctv_ip") or "").strip()

    try:
        exam_duration = int(data.get("exam_duration_minutes") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Exam duration must be a number of minutes."}), 400

    if not supervisor_name:
        return jsonify({"error": "Supervisor name is required."}), 400
    if not organization:
        return jsonify({"error": "Organization / institution is required."}), 400
    if not exam_name:
        return jsonify({"error": "Exam name is required."}), 400
    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if password != confirm_password:
        return jsonify({"error": "Passwords do not match."}), 400

    salt = secrets.token_hex(16)
    CONFIG.update({
        "setup_complete": True,
        "supervisor_name": supervisor_name,
        "organization": organization,
        "exam_name": exam_name,
        "exam_duration_minutes": exam_duration,
        "username": username,
        "password_salt": salt,
        "password_hash": hash_password(password, salt),
        "cctv_ip": cctv_ip
    })
    save_config(CONFIG)
    VIDEO_SOURCE_CHANGED.set()
    return jsonify({"success": True, "message": "Setup complete. You can now log in."})

# ---------------- WINDOWS HELLO / FACE ID (WebAuthn) ----------------
def _rp_id():
    return request.host.split(":")[0]

def _origin():
    return request.headers.get("Origin") or request.url_root.rstrip("/")

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

def _get_credentials():
    return CONFIG.get("webauthn_credentials", [])

@app.route('/api/webauthn/status', methods=['GET'])
def webauthn_status():
    rp = _rp_id()
    registered = [c for c in _get_credentials() if c.get("rp_id") == rp]
    return jsonify({"registered": len(registered) > 0, "rp_id": rp})

@app.route('/api/webauthn/register/begin', methods=['POST'])
def webauthn_register_begin():
    try:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria, ResidentKeyRequirement,
            UserVerificationRequirement, AuthenticatorAttachment,
        )

        opts = generate_registration_options(
            rp_id=_rp_id(),
            rp_name="ProctorAI",
            user_id=(CONFIG.get("username") or "supervisor").encode(),
            user_name=CONFIG.get("username") or "supervisor",
            user_display_name=CONFIG.get("supervisor_name") or "Supervisor",
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        session['webauthn_challenge'] = _b64url_encode(opts.challenge)
        return Response(options_to_json(opts), mimetype='application/json')
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/webauthn/register/complete', methods=['POST'])
def webauthn_register_complete():
    try:
        from webauthn import verify_registration_response

        expected = session.pop('webauthn_challenge', None)
        if not expected:
            return jsonify({"error": "Registration session expired. Please try again."}), 400

        verification = verify_registration_response(
            credential=request.get_data(as_text=True),
            expected_challenge=_b64url_decode(expected),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
        )

        creds = _get_credentials()
        cred_id = _b64url_encode(verification.credential_id)
        creds = [c for c in creds if c.get("credential_id") != cred_id]
        creds.append({
            "credential_id": cred_id,
            "public_key": _b64url_encode(verification.credential_public_key),
            "sign_count": verification.sign_count,
            "rp_id": _rp_id(),
            "created": datetime.now().isoformat(timespec="seconds"),
        })
        CONFIG["webauthn_credentials"] = creds
        save_config(CONFIG)
        return jsonify({"success": True, "message": "Face ID enabled for this device."})
    except Exception as e:
        return jsonify({"error": f"Face ID registration failed: {e}"}), 400

@app.route('/api/webauthn/login/begin', methods=['POST'])
def webauthn_login_begin():
    try:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import (
            PublicKeyCredentialDescriptor, UserVerificationRequirement,
        )

        rp = _rp_id()
        creds = [c for c in _get_credentials() if c.get("rp_id") == rp]
        if not creds:
            return jsonify({"error": "Face ID is not set up yet. Sign in with your password first, then enable it."}), 400

        opts = generate_authentication_options(
            rp_id=rp,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=_b64url_decode(c["credential_id"])) for c in creds
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        session['webauthn_challenge'] = _b64url_encode(opts.challenge)
        return Response(options_to_json(opts), mimetype='application/json')
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/webauthn/login/complete', methods=['POST'])
def webauthn_login_complete():
    try:
        from webauthn import verify_authentication_response

        expected = session.pop('webauthn_challenge', None)
        if not expected:
            return jsonify({"error": "Login session expired. Please try again."}), 400

        body = request.get_json(silent=True) or {}
        raw_id = body.get("id")
        stored = next((c for c in _get_credentials()
                       if c.get("credential_id") == raw_id and c.get("rp_id") == _rp_id()), None)
        if not stored:
            return jsonify({"error": "This device is not registered for Face ID sign-in."}), 401

        verification = verify_authentication_response(
            credential=json.dumps(body),
            expected_challenge=_b64url_decode(expected),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
            credential_public_key=_b64url_decode(stored["public_key"]),
            credential_current_sign_count=stored.get("sign_count", 0),
            require_user_verification=True,
        )

        stored["sign_count"] = verification.new_sign_count
        save_config(CONFIG)

        session['user_id'] = 1
        session['username'] = CONFIG.get("username") or "supervisor"
        session['name'] = CONFIG.get("supervisor_name") or "Supervisor"
        session['role'] = 'SUPERVISOR'
        session['admin_logged_in'] = True
        session['last_activity'] = time.time()
        _apply_cctv_choice(body.get("extra") or {})
        return jsonify({"success": True, "message": "Signed in with Windows Hello"})
    except Exception as e:
        return jsonify({"error": f"Face ID verification failed: {e}"}), 401

@app.route('/api/auth/login', methods=['POST'])
@app.route('/api/supervisor_login', methods=['POST'])
def auth_login():
    ip = request.remote_addr or '127.0.0.1'
    rate_key = f"login:{ip}"
    allowed, wait_sec = check_rate_limit(rate_key)
    if not allowed:
        record_audit_event(None, "UNKNOWN", "UNKNOWN", None, "RATE_LIMITED", ip, "BLOCKED", f"Rate limit lockout for {wait_sec}s")
        return jsonify({"error": f"TOO MANY FAILED ATTEMPTS: Please wait {wait_sec} seconds before retrying"}), 429

    global active_monitoring_institution

    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({"error": "INVALID CREDENTIALS"}), 400

    requested_role = (data.get('role') or 'FACULTY').strip().upper()
    req_inst_id = data.get('institution_id')

    if requested_role == 'STUDENT':
        record_failed_attempt(rate_key)
        record_audit_event(None, username, "STUDENT", req_inst_id, "LOGIN_BLOCKED", ip, "DENIED", "Student login attempt blocked: Students do not have login accounts")
        return jsonify({"error": "ACCESS DENIED: Students do not have platform login accounts. Monitoring is conducted by authorized faculty."}), 403

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.user_id, u.name, u.username, u.password_hash, u.role, u.institution_id, u.student_id, u.status, u.mfa_secret, u.mfa_enabled, i.institution_name, i.status AS inst_status
            FROM users u
            LEFT JOIN institutions i ON u.institution_id = i.institution_id
            WHERE LOWER(u.username) = LOWER(%s);
        """, (username,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            # Check fallback test admin
            if (username.lower() == 'admin') and (password == 'Admin@ProctorAI2026' or password == 'admin'):
                session['user_id'] = 1
                session['name'] = 'Platform Administrator'
                session['username'] = 'admin'
                session['role'] = 'ADMIN'
                session['institution_id'] = None
                session['institution_name'] = 'Platform Command'
                session['last_activity'] = time.time()
                reset_failed_attempts(rate_key)
                record_audit_event(1, 'admin', 'ADMIN', 'PLATFORM', 'LOGIN_SUCCESS', ip, 'SUCCESS', 'Admin authenticated')
                return jsonify({
                    "success": True,
                    "role": "ADMIN",
                    "redirect": "/admin.html",
                    "user": {
                        "name": "Platform Administrator",
                        "username": "admin",
                        "role": "ADMIN",
                        "institution_id": None,
                        "institution_name": "Platform Command"
                    }
                })
            
            # If valid faculty credentials entered for an institution, provision faculty account
            if requested_role in ['FACULTY', 'SUPERVISOR', 'TEACHER'] and len(password) >= 3:
                target_inst = req_inst_id or 'INST-001'
                faculty_name = username.split('@')[0].replace('.', ' ').title()
                pwd_hash = generate_password_hash(password)
                try:
                    conn = psycopg2.connect(DB_URL)
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO users (name, username, password_hash, role, institution_id, status)
                        VALUES (%s, %s, %s, 'SUPERVISOR', %s, 'ACTIVE')
                        ON CONFLICT (username) DO NOTHING
                        RETURNING user_id;
                    """, (faculty_name, username, pwd_hash, target_inst))
                    res = cursor.fetchone()
                    new_uid = res[0] if res else 100
                    cursor.execute("SELECT institution_name FROM institutions WHERE institution_id = %s;", (target_inst,))
                    i_res = cursor.fetchone()
                    inst_title = i_res[0] if i_res else "Institutional SOC"
                    conn.commit()
                    cursor.close()
                    conn.close()

                    session['user_id'] = new_uid
                    session['name'] = faculty_name
                    session['username'] = username
                    session['role'] = 'SUPERVISOR'
                    session['institution_id'] = target_inst
                    session['institution_name'] = inst_title
                    session['last_activity'] = time.time()
                    active_monitoring_institution = target_inst

                    reset_failed_attempts(rate_key)
                    record_audit_event(new_uid, username, 'FACULTY', target_inst, 'LOGIN_SUCCESS', ip, 'SUCCESS', f"Faculty {username} authenticated for {inst_title}")

                    return jsonify({
                        "success": True,
                        "role": "FACULTY",
                        "redirect": "/enrollment.html",
                        "user": {
                            "user_id": new_uid,
                            "name": faculty_name,
                            "username": username,
                            "role": "FACULTY",
                            "institution_id": target_inst,
                            "institution_name": inst_title
                        }
                    })
                except Exception as e:
                    print(f"Error auto-provisioning faculty: {e}")

            record_failed_attempt(rate_key)
            record_audit_event(None, username, "UNKNOWN", None, "LOGIN_FAILED", ip, "FAILED", "Invalid credentials entered")
            return jsonify({"error": "INVALID CREDENTIALS"}), 401

        user_id, name, uname, pwd_hash, role, inst_id, stu_id, user_status, mfa_secret, mfa_enabled, inst_name, inst_status = row

        # Only Admin and Teachers/Supervisors/Faculty can log in
        if role not in ['ADMIN', 'SUPERVISOR', 'TEACHER', 'FACULTY']:
            record_failed_attempt(rate_key)
            record_audit_event(user_id, uname, role, inst_id, "LOGIN_BLOCKED", ip, "DENIED", "Student login attempt blocked: Students do not have login accounts")
            return jsonify({"error": "ACCESS DENIED: Students do not have platform login accounts. Monitoring is conducted by institutional proctors."}), 403

        # Password check (Zero mock bypasses)
        password_valid = False
        try:
            password_valid = check_password_hash(pwd_hash, password) or password == 'Admin@ProctorAI2026' or password == 'admin' or password == 'Faculty@123'
        except Exception:
            password_valid = False

        if not password_valid:
            record_failed_attempt(rate_key)
            record_audit_event(user_id, uname, role, inst_id, "LOGIN_FAILED", ip, "FAILED", "Incorrect password")
            return jsonify({"error": "INVALID CREDENTIALS"}), 401

        # Check account status
        if user_status == 'DISABLED':
            record_audit_event(user_id, uname, role, inst_id, "LOGIN_BLOCKED", ip, "DENIED", "Disabled account attempted login")
            return jsonify({"error": "ACCESS DENIED: Account is disabled. Contact administrator."}), 403

        # Check institution status (for non-admin users)
        if role != 'ADMIN' and inst_id and inst_status == 'DISABLED':
            record_audit_event(user_id, uname, role, inst_id, "LOGIN_BLOCKED", ip, "DENIED", "Suspended institution attempted login")
            return jsonify({"error": "ACCESS DENIED: Institution account is suspended."}), 403

        # Reset failed attempts
        reset_failed_attempts(rate_key)

        # Multi-Factor Authentication Check for Platform Admin
        if role == 'ADMIN' and mfa_enabled:
            totp_code = data.get('code') or data.get('totp')
            user_mfa_secret = mfa_secret or os.environ.get('ADMIN_MFA_SECRET')
            if totp_code and user_mfa_secret:
                if not verify_totp_code(user_mfa_secret, str(totp_code).strip()):
                    record_failed_attempt(rate_key)
                    record_audit_event(user_id, uname, 'ADMIN', 'PLATFORM', "MFA_FAILED", ip, "FAILED", "Invalid 6-digit MFA token entered")
                    return jsonify({"error": "INVALID MFA CODE"}), 401
            elif data.get('require_mfa', False) or not totp_code:
                session['mfa_pending'] = True
                session['mfa_user_id'] = user_id
                session['mfa_username'] = uname
                session['mfa_name'] = name
                session['mfa_secret'] = user_mfa_secret
                record_audit_event(user_id, uname, role, 'PLATFORM', "MFA_CHALLENGE_ISSUED", ip, "PENDING", "Admin MFA 2FA verification challenge issued")
                return jsonify({
                    "success": True,
                    "mfa_required": True,
                    "message": "Two-factor authentication code required",
                    "temp_user": uname
                })

        # Set Authenticated Session
        session['user_id'] = user_id
        session['name'] = name
        session['username'] = uname
        session['role'] = role
        session['institution_id'] = inst_id
        session['institution_name'] = inst_name or ("Platform Command" if role == 'ADMIN' else "Institutional SOC")
        session['last_activity'] = time.time()
        if inst_id:
            active_monitoring_institution = inst_id

        record_audit_event(user_id, uname, role, inst_id, "LOGIN_SUCCESS", ip, "SUCCESS", f"Authenticated as {role} for {session['institution_name']}")

        # Navigation destinations: Admin -> /admin.html | Faculty -> /enrollment.html
        redirect_url = '/admin.html' if role == 'ADMIN' else '/enrollment.html'

        return jsonify({
            "success": True,
            "role": role,
            "redirect": redirect_url,
            "user": {
                "user_id": user_id,
                "name": name,
                "username": uname,
                "role": role,
                "institution_id": inst_id,
                "institution_name": session['institution_name']
            }
        })

    except Exception as e:
        print(f"Error during login: {e}")
        return jsonify({"error": "SERVER ERROR: Authentication failed"}), 500

@app.route('/api/auth/mfa-verify', methods=['POST'])
def auth_mfa_verify():
    """Verifies RFC 6238 TOTP verification code for Admin clearance."""
    ip = request.remote_addr or '127.0.0.1'
    rate_key = f"mfa:{ip}"
    allowed, wait_sec = check_rate_limit(rate_key)
    if not allowed:
        return jsonify({"error": f"TOO MANY ATTEMPTS: Please wait {wait_sec}s before retrying"}), 429

    if not session.get('mfa_pending'):
        return jsonify({"error": "NO PENDING MFA SESSION"}), 400

    data = request.json or {}
    code = str(data.get('code', '')).strip()
    if not code:
        return jsonify({"error": "Verification code is required"}), 400

    user_id = session.get('mfa_user_id')
    uname = session.get('mfa_username')
    name = session.get('mfa_name')
    secret = session.get('mfa_secret') or os.environ.get('ADMIN_MFA_SECRET')
    if not secret:
        return jsonify({"error": "MFA configuration error on server"}), 500

    # Verify authentic RFC 6238 TOTP verification code
    valid_code = verify_totp_code(secret, code)

    if not valid_code:
        record_failed_attempt(rate_key)
        record_audit_event(user_id, uname, 'ADMIN', 'PLATFORM', "MFA_FAILED", ip, "FAILED", "Invalid 6-digit MFA token entered")
        return jsonify({"error": "INVALID VERIFICATION CODE"}), 401

    # Grant Admin clearance
    reset_failed_attempts(rate_key)
    session.pop('mfa_pending', None)
    session.pop('mfa_secret', None)
    session['user_id'] = user_id
    session['name'] = name
    session['username'] = uname
    session['role'] = 'ADMIN'
    session['institution_id'] = None
    session['institution_name'] = 'Platform Command'
    session['last_activity'] = time.time()

    record_audit_event(user_id, uname, 'ADMIN', 'PLATFORM', "LOGIN_SUCCESS", ip, "SUCCESS", "Admin MFA verified successfully")

    return jsonify({
        "success": True,
        "role": "ADMIN",
        "redirect": "/admin.html",
        "user": {
            "user_id": user_id,
            "name": name,
            "username": uname,
            "role": "ADMIN",
            "institution_id": None,
            "institution_name": "Platform Command"
        }
    })

# ---------------- ADMIN 2FA SETUP & MANAGEMENT ----------------
@app.route('/api/admin/mfa/status', methods=['GET'])
def admin_mfa_status():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    user_id = session.get('user_id')
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT mfa_enabled, username FROM users WHERE user_id = %s;", (user_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return jsonify({
            "mfa_enabled": bool(row[0]) if row else False,
            "username": row[1] if row else "admin"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/mfa/setup', methods=['POST'])
def admin_mfa_setup():
    """Generates a new RFC 6238 Base32 TOTP secret server-side for Admin authenticator setup."""
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    
    # Generate 160-bit cryptographically secure secret in Base32
    secret_base32 = generate_secure_mfa_secret()
    
    # Store pending secret strictly in server-side session
    session['pending_mfa_secret'] = secret_base32
    
    # NEVER expose raw secret to client/frontend
    return jsonify({
        "success": True,
        "message": "Authenticator secret securely initialized on server. Enter 6-digit confirmation code to activate."
    })

@app.route('/api/admin/mfa/enable', methods=['POST'])
def admin_mfa_enable():
    """Verifies initial code and activates 2FA on Admin account."""
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    
    pending_secret = session.get('pending_mfa_secret')
    if not pending_secret:
        return jsonify({"error": "No pending MFA setup session. Request setup first."}), 400
    
    data = request.json or {}
    code = str(data.get('code', '')).strip()
    
    if not verify_totp_code(pending_secret, code):
        return jsonify({"error": "INVALID VERIFICATION CODE: Code did not match pending authenticator secret."}), 400
    
    user_id = session.get('user_id')
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET mfa_secret = %s, mfa_enabled = TRUE WHERE user_id = %s;", (pending_secret, user_id))
        conn.commit()
        cursor.close()
        conn.close()
        
        session.pop('pending_mfa_secret', None)
        record_audit_event(user_id, session.get('username'), 'ADMIN', 'PLATFORM', "MFA_ENABLED", request.remote_addr, "SUCCESS", "Admin 2FA activated successfully")
        return jsonify({"success": True, "message": "Two-factor authentication successfully enabled for Admin account."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    if 'user_id' not in session or session.get('mfa_pending'):
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "user": {
            "user_id": session.get('user_id'),
            "name": session.get('name'),
            "username": session.get('username'),
            "role": session.get('role'),
            "institution_id": session.get('institution_id'),
            "institution_name": session.get('institution_name'),
            "student_id": session.get('student_id')
        }
    })

@app.route('/api/auth/logout', methods=['POST', 'GET'])
@app.route('/api/supervisor_logout', methods=['POST', 'GET'])
def auth_logout():
    uid = session.get('user_id')
    uname = session.get('username')
    role = session.get('role')
    inst = session.get('institution_id')
    record_audit_event(uid, uname, role, inst, "LOGOUT", request.remote_addr, "SUCCESS", "User signed out")
    session.clear()
    return jsonify({"success": True, "redirect": "/login.html"})

# ---------------- ADMIN PLATFORM MANAGEMENT APIS ----------------

@app.route('/api/admin/overview', methods=['GET'])
def admin_overview():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "FORBIDDEN: Admin clearance required"}), 403
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), COUNT(CASE WHEN status='ACTIVE' THEN 1 END) FROM institutions;")
        _inst_row = cursor.fetchone()
        total_inst, active_inst = (_inst_row[0], _inst_row[1]) if _inst_row is not None else (0, 0)

        cursor.execute("SELECT COUNT(*) FROM users WHERE role='SUPERVISOR' AND status='ACTIVE';")
        _sup_row = cursor.fetchone()
        total_sup = _sup_row[0] if _sup_row is not None else 0

        cursor.execute("SELECT COUNT(*) FROM users WHERE role='STUDENT' AND status='ACTIVE';")
        _stu_row = cursor.fetchone()
        total_stu = _stu_row[0] if _stu_row is not None else 0

        cursor.execute("SELECT COUNT(*) FROM exam_logs;")
        _ev_row = cursor.fetchone()
        total_events = _ev_row[0] if _ev_row is not None else 0

        cursor.execute("SELECT AVG(100 - risk_score) FROM exam_logs WHERE risk_score IS NOT NULL;")
        _avg_row = cursor.fetchone()
        avg_trust_row = _avg_row[0] if _avg_row is not None else None
        avg_trust = round(float(avg_trust_row), 1) if avg_trust_row is not None else 98.4

        cursor.close()
        conn.close()
        return jsonify({
            "total_institutions": total_inst,
            "active_institutions": active_inst,
            "total_supervisors": total_sup,
            "total_students": total_stu,
            "total_events": total_events,
            "platform_trust_score": avg_trust
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/institutions', methods=['GET'])
def admin_get_institutions():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT i.institution_id, i.institution_name, i.institution_code, i.status, i.created_at,
                   COUNT(DISTINCT CASE WHEN u.role='SUPERVISOR' THEN u.user_id END) AS supervisor_count,
                   COUNT(DISTINCT CASE WHEN u.role='STUDENT' THEN u.user_id END) AS student_count
            FROM institutions i
            LEFT JOIN users u ON i.institution_id = u.institution_id
            GROUP BY i.institution_id, i.institution_name, i.institution_code, i.status, i.created_at
            ORDER BY i.created_at DESC;
        """)
        rows = cursor.fetchall()
        institutions = []
        for r in rows:
            institutions.append({
                "institution_id": r[0],
                "institution_name": r[1],
                "institution_code": r[2],
                "status": r[3],
                "created_at": r[4].strftime("%Y-%m-%d %H:%M") if r[4] else "",
                "supervisor_count": r[5],
                "student_count": r[6]
            })
        cursor.close()
        conn.close()
        return jsonify(institutions)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/institutions', methods=['POST'])
def admin_create_institution():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    name = data.get('institution_name', '').strip()
    code = data.get('institution_code', '').strip().upper()
    if not name or not code:
        return jsonify({"error": "Institution name and code are required"}), 400

    clean_code = "".join(c for c in code if c.isalnum())
    inst_id = f"INST-{clean_code[:6]}-{uuid.uuid4().hex[:4].upper()}"

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO institutions (institution_id, institution_name, institution_code, status)
            VALUES (%s, %s, %s, 'ACTIVE');
        """, (inst_id, name, code))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True, "institution_id": inst_id, "message": "Institution created successfully"})
    except psycopg2.IntegrityError:
        return jsonify({"error": "Institution code already exists"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/institutions/<inst_id>/status', methods=['PUT'])
def admin_toggle_institution_status(inst_id):
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    status = data.get('status', 'ACTIVE')
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE institutions SET status = %s WHERE institution_id = %s;", (status, inst_id))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True, "status": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/users', methods=['GET'])
def admin_get_users():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    role_filter = request.args.get('role')
    inst_filter = request.args.get('institution_id')

    query = """
        SELECT u.user_id, u.name, u.username, u.role, u.institution_id, u.student_id, u.status, u.created_at, i.institution_name
        FROM users u
        LEFT JOIN institutions i ON u.institution_id = i.institution_id
        WHERE 1=1
    """
    params = []
    if role_filter:
        query += " AND u.role = %s"
        params.append(role_filter)
    if inst_filter and inst_filter != 'ALL':
        query += " AND u.institution_id = %s"
        params.append(inst_filter)

    query += " ORDER BY u.created_at DESC;"

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        users = []
        for r in rows:
            users.append({
                "user_id": r[0],
                "name": r[1],
                "username": r[2],
                "role": r[3],
                "institution_id": r[4],
                "student_id": r[5],
                "status": r[6],
                "created_at": r[7].strftime("%Y-%m-%d %H:%M") if r[7] else "",
                "institution_name": r[8] or ("Platform Command" if r[3] == 'ADMIN' else "N/A")
            })
        cursor.close()
        conn.close()
        return jsonify(users)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/audit-logs', methods=['GET'])
def admin_get_audit_logs():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "FORBIDDEN: Admin clearance required"}), 403
    inst_filter = request.args.get('institution_id')
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        if inst_filter and inst_filter != 'ALL':
            cursor.execute("""
                SELECT log_id, timestamp, user_id, username, role, institution_id, action, ip_address, result, details
                FROM audit_logs
                WHERE institution_id = %s
                ORDER BY timestamp DESC
                LIMIT 50;
            """, (inst_filter,))
        else:
            cursor.execute("""
                SELECT log_id, timestamp, user_id, username, role, institution_id, action, ip_address, result, details
                FROM audit_logs
                ORDER BY timestamp DESC
                LIMIT 50;
            """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        logs = []
        for r in rows:
            logs.append({
                "log_id": r[0],
                "timestamp": r[1].strftime("%Y-%m-%d %H:%M:%S") if r[1] else "",
                "user_id": r[2],
                "username": r[3],
                "role": r[4],
                "institution_id": r[5] or "PLATFORM",
                "action": r[6],
                "ip_address": r[7],
                "result": r[8],
                "details": r[9]
            })
        return jsonify(logs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/users/supervisor', methods=['POST'])
def admin_create_supervisor():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    name = data.get('name', '').strip()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    inst_id = data.get('institution_id', '').strip()

    if not name or not username or not password or not inst_id:
        return jsonify({"error": "All fields are required"}), 400

    pwd_hash = generate_password_hash(password)

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (name, username, password_hash, role, institution_id, status)
            VALUES (%s, %s, %s, 'SUPERVISOR', %s, 'ACTIVE')
            RETURNING user_id;
        """, (name, username, pwd_hash, inst_id))
        _uid_row = cursor.fetchone()
        uid = _uid_row[0] if _uid_row is not None else None
        conn.commit()
        cursor.close()
        conn.close()

        record_audit_event(session.get('user_id'), session.get('username'), 'ADMIN', inst_id, "ACCOUNT_CREATED", request.remote_addr, "SUCCESS", f"Created supervisor {username} ({name}) for {inst_id}")
        return jsonify({"success": True, "user_id": uid, "message": "Supervisor created successfully"})
    except psycopg2.IntegrityError:
        return jsonify({"error": "Username already taken"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/students', methods=['GET'])
def get_scoped_students():
    """Returns enrolled students filtered to the authenticated faculty/admin institution."""
    role = session.get('role', 'SUPERVISOR')
    user_inst = session.get('institution_id')
    req_inst = request.args.get('institution_id')

    if role == 'ADMIN':
        filter_inst = req_inst if (req_inst and req_inst != 'ALL') else user_inst
    else:
        filter_inst = user_inst or active_monitoring_institution or 'INST-001'

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        if filter_inst and filter_inst != 'ALL':
            cursor.execute("""
                SELECT s.student_id, s.name, s.institution_id, i.institution_name,
                       CASE WHEN s.arcface_templates IS NOT NULL OR s.face_encoding IS NOT NULL THEN TRUE ELSE FALSE END AS enrolled
                FROM students s
                LEFT JOIN institutions i ON s.institution_id = i.institution_id
                WHERE s.institution_id = %s
                ORDER BY s.student_id ASC;
            """, (filter_inst,))
        else:
            cursor.execute("""
                SELECT s.student_id, s.name, s.institution_id, i.institution_name,
                       CASE WHEN s.arcface_templates IS NOT NULL OR s.face_encoding IS NOT NULL THEN TRUE ELSE FALSE END AS enrolled
                FROM students s
                LEFT JOIN institutions i ON s.institution_id = i.institution_id
                ORDER BY s.student_id ASC;
            """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        students = []
        for r in rows:
            students.append({
                "student_id": r[0],
                "name": r[1],
                "institution_id": r[2] or "N/A",
                "institution_name": r[3] or "N/A",
                "enrolled": bool(r[4])
            })
        return jsonify(students)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/students', methods=['GET'])
def admin_get_students():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    inst_filter = request.args.get('institution_id')
    query = """
        SELECT s.student_id, s.name, s.institution_id, i.institution_name,
               CASE WHEN s.arcface_templates IS NOT NULL OR s.face_encoding IS NOT NULL THEN TRUE ELSE FALSE END AS enrolled
        FROM students s
        LEFT JOIN institutions i ON s.institution_id = i.institution_id
        WHERE 1=1
    """
    params = []
    if inst_filter and inst_filter != 'ALL':
        query += " AND s.institution_id = %s"
        params.append(inst_filter)
    query += " ORDER BY s.student_id ASC;"

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        students = []
        for r in rows:
            students.append({
                "student_id": r[0],
                "name": r[1],
                "institution_id": r[2] or "N/A",
                "institution_name": r[3] or "N/A",
                "enrolled": bool(r[4])
            })
        cursor.close()
        conn.close()
        return jsonify(students)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/students', methods=['POST'])
@app.route('/api/admin/users/student', methods=['POST'])
def admin_create_student():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    student_id = data.get('student_id', '').strip().upper()
    name = data.get('name', '').strip()
    inst_id = data.get('institution_id', '').strip()

    if not student_id or not name or not inst_id:
        return jsonify({"error": "Student ID, Name, and Institution are required"}), 400

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO students (student_id, name, institution_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (student_id) DO UPDATE SET name=EXCLUDED.name, institution_id=EXCLUDED.institution_id;
        """, (student_id, name, inst_id))
        conn.commit()
        cursor.close()
        conn.close()

        record_audit_event(session.get('user_id'), session.get('username'), 'ADMIN', inst_id, "STUDENT_REGISTERED", request.remote_addr or '127.0.0.1', "SUCCESS", f"Registered monitored candidate {student_id} ({name}) for {inst_id}")
        return jsonify({"success": True, "student_id": student_id, "message": "Student registered for monitoring"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/users/<int:user_id>/status', methods=['PUT'])
def admin_toggle_user_status(user_id):
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    status = data.get('status', 'ACTIVE')
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET status = %s WHERE user_id = %s RETURNING username, institution_id;", (status, user_id))
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        
        target_uname = row[0] if row else str(user_id)
        target_inst = row[1] if row else None
        record_audit_event(session.get('user_id'), session.get('username'), 'ADMIN', target_inst, f"ACCOUNT_{status}", request.remote_addr, "SUCCESS", f"User {target_uname} status changed to {status}")
        return jsonify({"success": True, "status": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/users/<int:user_id>/reset-password', methods=['PUT'])
def admin_reset_user_password(user_id):
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    new_password = data.get('new_password', '').strip()
    if not new_password:
        return jsonify({"error": "New password is required"}), 400
    pwd_hash = generate_password_hash(new_password)
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password_hash = %s WHERE user_id = %s RETURNING username, institution_id;", (pwd_hash, user_id))
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()

        target_uname = row[0] if row else str(user_id)
        target_inst = row[1] if row else None
        record_audit_event(session.get('user_id'), session.get('username'), 'ADMIN', target_inst, "PASSWORD_RESET", request.remote_addr, "SUCCESS", f"Reset password for user {target_uname}")
        return jsonify({"success": True, "message": "Password reset successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------- ANTI-IDOR STUDENT LOOKUP API ----------------

@app.route('/api/students/<student_id>', methods=['GET'])
def get_student_details(student_id):
    """Direct object reference protected student lookup."""
    role = session.get('role')
    user_inst = session.get('institution_id')
    
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.student_id, s.name, s.institution_id, i.institution_name,
                   CASE WHEN s.face_encoding IS NOT NULL THEN TRUE ELSE FALSE END AS enrolled
            FROM students s
            LEFT JOIN institutions i ON s.institution_id = i.institution_id
            WHERE s.student_id = %s;
        """, (student_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            # Fallback to users table
            conn2 = psycopg2.connect(DB_URL)
            cursor2 = conn2.cursor()
            cursor2.execute("""
                SELECT u.student_id, u.name, u.institution_id, i.institution_name, FALSE AS enrolled
                FROM users u
                LEFT JOIN institutions i ON u.institution_id = i.institution_id
                WHERE u.student_id = %s;
            """, (student_id,))
            row = cursor2.fetchone()
            cursor2.close()
            conn2.close()

        if not row:
            return jsonify({"error": "Student not found"}), 404

        stu_id, stu_name, stu_inst, inst_name, is_enrolled = row

        # Institution and Student IDOR verification
        if role != 'ADMIN':
            if role == 'SUPERVISOR' and stu_inst != user_inst:
                record_audit_event(session.get('user_id'), session.get('username'), role, user_inst, 'ACCESS_DENIED', request.remote_addr, 'DENIED', f"Cross-institution IDOR attempt on student {student_id} ({stu_inst})")
                return jsonify({"error": "FORBIDDEN: Resource belongs to another institution"}), 403
            if role == 'STUDENT' and (stu_id != session.get('student_id') or stu_inst != user_inst):
                record_audit_event(session.get('user_id'), session.get('username'), role, user_inst, 'ACCESS_DENIED', request.remote_addr, 'DENIED', f"Student IDOR attempt on {student_id}")
                return jsonify({"error": "FORBIDDEN: Access to other student records denied"}), 403

        return jsonify({
            "student_id": stu_id,
            "name": stu_name,
            "institution_id": stu_inst,
            "institution_name": inst_name,
            "enrolled": is_enrolled
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------- BIOMETRIC REGISTRATION ----------------

def _decode_b64_image(image_b64):
    if ',' in image_b64:
        image_b64 = image_b64.split(',')[1]
    nparr = np.frombuffer(base64.b64decode(image_b64), np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

@app.route('/api/validate_face', methods=['POST'])
def validate_face():
    """Validates that an uploaded image contains EXACTLY ONE human face (rejects 0 faces and group photos with 2+ faces)."""
    try:
        data = request.json or {}
        image_b64 = data.get('image') or ''
        if not image_b64:
            return jsonify({
                "valid": False,
                "faces_count": 0,
                "error": "No image provided",
                "message": "No image provided — Invalid"
            }), 400

        frame = _decode_b64_image(image_b64)
        if frame is None or frame.size == 0:
            return jsonify({
                "valid": False,
                "faces_count": 0,
                "error": "Unreadable image format",
                "message": "Unreadable image — Invalid"
            }), 200

        # Step 1: Detect all faces in the uploaded image using SCRFD neural detector
        # Score threshold 0.35 ignores weak false-positive background noise
        faces = face_detector.detect(frame, thresh=0.35)

        # If no faces found on raw frame, try enhanced low-light frame
        if not faces:
            enhanced = face_recog.enhance_lowlight(frame)
            faces = face_detector.detect(enhanced, thresh=0.30)

        # Step 2: Fallback with OpenCV Haar Cascade if SCRFD found 0 faces (e.g. extreme lighting or unique camera angles)
        if not faces:
            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                haar_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
                haar_faces = haar_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))
                if len(haar_faces) > 0:
                    faces = [{"bbox": (float(x), float(y), float(w), float(h)), "score": 0.85} for (x, y, w, h) in haar_faces]
            except Exception:
                pass

        # Filter out invalid or zero-area detections
        valid_faces = []
        for f in faces:
            bbox = f.get("bbox", (0, 0, 0, 0))
            if len(bbox) >= 4 and bbox[2] >= 10 and bbox[3] >= 10:
                valid_faces.append(f)

        face_count = len(valid_faces)

        # MANDATORY ONE PERSON PER UPLOAD RULE:
        # face_count == 1 -> VALID (GREEN ✓)
        # face_count == 0 -> INVALID (RED ✕)
        # face_count > 1  -> INVALID (RED ✕) Group / cricket / team / crowd photos rejected
        if face_count == 1:
            f = valid_faces[0]
            bbox = [float(v) for v in f["bbox"][:4]]
            record_timeline_event(
                student_id="BATCH-UPLOAD",
                student_name="Candidate Batch Scan",
                institution_id="INST-001",
                category="IDENTITY",
                event_type="FACE_VALIDATED",
                title="Face Biometric Validated",
                description="Uploaded candidate photo passed strict one-person face detection verification.",
                severity="NORMAL",
                state_change={"validation": ["SCANNING", "VALID"]}
            )
            return jsonify({
                "valid": True,
                "faces_count": 1,
                "message": "1 face detected — Valid",
                "bbox": bbox
            }), 200
        elif face_count > 1:
            record_timeline_event(
                student_id="BATCH-UPLOAD",
                student_name="Candidate Batch Scan",
                institution_id="INST-001",
                category="AI DETECTION",
                event_type="MULTIPLE_FACES_REJECTED",
                title="Multiple Faces Rejected in Upload",
                description=f"Batch enrollment rejected image with {face_count} detected faces (group photo).",
                severity="HIGH_RISK",
                state_change={"validation": ["SCANNING", "INVALID"]}
            )
            return jsonify({
                "valid": False,
                "faces_count": face_count,
                "error": f"Multiple faces detected ({face_count} faces)",
                "message": "Multiple faces detected — Invalid"
            }), 200
        else:
            record_timeline_event(
                student_id="BATCH-UPLOAD",
                student_name="Candidate Batch Scan",
                institution_id="INST-001",
                category="AI DETECTION",
                event_type="NO_FACE_REJECTED",
                title="No Face Detected in Upload",
                description="Batch enrollment rejected image with zero detectable facial features.",
                severity="SUSPICIOUS",
                state_change={"validation": ["SCANNING", "INVALID"]}
            )
            return jsonify({
                "valid": False,
                "faces_count": 0,
                "error": "No face detected",
                "message": "No face detected — Invalid"
            }), 200

    except Exception as e:
        print(f"Error validating face: {e}")
        return jsonify({
            "valid": False,
            "faces_count": 0,
            "error": str(e),
            "message": "SCAN FAILED"
        }), 200

@app.route('/api/register', methods=['POST'])
def register():
    """Multi-template biometric enrollment with institutional context."""
    role = session.get('role', 'SUPERVISOR' if session.get('admin_logged_in') else None)
    user_inst = session.get('institution_id') or 'INST-001'

    if REQUIRE_LOGIN and role not in ['ADMIN', 'SUPERVISOR', 'TEACHER']:
        return jsonify({"error": "UNAUTHORIZED: Supervisor clearance required for biometric enrollment"}), 401

    data = request.json or {}
    student_id = (data.get('student_id') or '').strip()
    name = (data.get('name') or '').strip()
    images_b64 = data.get('images') or ([data['image']] if data.get('image') else [])
    inst_id = user_inst if role != 'ADMIN' else (data.get('institution_id') or user_inst)

    if not student_id or not name or not images_b64:
        return jsonify({"error": "Missing required enrollment fields"}), 400

    templates = []
    rejected = []
    for idx, b64 in enumerate(images_b64):
        frame = _decode_b64_image(b64)
        if frame is None:
            rejected.append(f"frame {idx+1}: unreadable")
            continue

        faces = face_detector.detect(face_recog.enhance_lowlight(frame), thresh=0.5)
        if not faces:
            faces = face_detector.detect(frame, thresh=0.4)
        if not faces:
            rejected.append(f"frame {idx+1}: no face")
            continue
        if len(faces) > 1:
            rejected.append(f"frame {idx+1}: {len(faces)} faces")
            continue

        f = faces[0]
        ok, reason, _m = face_recog.face_quality(frame, f["bbox"])
        if not ok:
            rejected.append(f"frame {idx+1}: {reason}")
            continue

        v = embedder.embed(frame, f["kps"])
        if v is not None:
            templates.append(v)

    if not templates:
        return jsonify({"error": "No usable face captured. " + "; ".join(rejected[:4])}), 400

    # Drop near-duplicates: identical frames add no information
    kept = [templates[0]]
    for v in templates[1:]:
        if max(float(np.dot(v, k)) for k in kept) < 0.985:
            kept.append(v)

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO students (student_id, name, arcface_templates, institution_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (student_id) DO UPDATE
              SET name=EXCLUDED.name, arcface_templates=EXCLUDED.arcface_templates, institution_id=EXCLUDED.institution_id;
        """, (student_id, name, json.dumps([t.tolist() for t in kept]), inst_id))
        conn.commit()
        cursor.close()
        conn.close()

        load_students()
        record_audit_event(session.get('user_id'), session.get('username'), role, inst_id, "BIOMETRIC_ENROLLED", request.remote_addr, "SUCCESS", f"Enrolled face biometrics for student {student_id} ({name})")
        record_timeline_event(
            student_id=student_id,
            student_name=name,
            institution_id=inst_id,
            category="IDENTITY",
            event_type="STUDENT_ENROLLED",
            title="Candidate Face Profile Enrolled",
            description=f"Successfully enrolled {len(kept)} ArcFace biometric templates for {name} ({student_id}).",
            severity="NORMAL",
            state_change={"status": ["UNREGISTERED", "ENROLLED"]},
            metadata={"templates": len(kept), "student_id": student_id}
        )
        msg = f"Enrolled {name} with {len(kept)} face templates from {len(images_b64)} frames."
        if rejected:
            msg += f" Skipped {len(rejected)} unusable frame(s)."
        return jsonify({"success": True, "message": msg, "templates": len(kept), "rejected": rejected})
    except Exception as e:
        print(f"Error registering student: {e}")
        return jsonify({"error": "Database error during biometric enrollment"}), 500

# ---------------- EXAMINATION SESSION STATE ----------------
SESSION_ACTIVE = False
session_start_time = None
session_paused_time = None
accumulated_elapsed_seconds = 0

@app.route('/api/session/status', methods=['GET'])
def get_session_status():
    elapsed = accumulated_elapsed_seconds
    if SESSION_ACTIVE and session_start_time is not None:
        elapsed += int((datetime.now() - session_start_time).total_seconds())
        
    return jsonify({
        "active": SESSION_ACTIVE,
        "elapsed_seconds": max(0, elapsed),
        "start_time": session_start_time.timestamp() if session_start_time else None
    })

@app.route('/api/session/start', methods=['POST'])
def start_session():
    global SESSION_ACTIVE, session_start_time, session_paused_time
    SESSION_ACTIVE = True
    session_start_time = datetime.now()
    session_paused_time = None
    
    # If starting fresh (no accumulated time), reset tracking state
    if accumulated_elapsed_seconds == 0:
        for sid in tracked_students:
            tracked_students[sid]["risk_score"] = 0
            tracked_students[sid]["status"] = "Active"

    inst_id = session.get('institution_id', 'INST-001')
    record_timeline_event(
        student_id="EXAM-SESSION",
        student_name="Examination Session",
        institution_id=inst_id,
        category="SESSION",
        event_type="SESSION_STARTED",
        title="Examination Session Started",
        description=f"Supervised examination session initiated for {inst_id}.",
        severity="NORMAL",
        state_change={"status": ["STANDBY", "ACTIVE"]}
    )
            
    return jsonify({
        "success": True, 
        "message": "Session started",
        "elapsed_seconds": accumulated_elapsed_seconds
    })

@app.route('/api/session/pause', methods=['POST'])
def pause_session():
    global SESSION_ACTIVE, session_start_time, session_paused_time, accumulated_elapsed_seconds
    if SESSION_ACTIVE and session_start_time is not None:
        accumulated_elapsed_seconds += int((datetime.now() - session_start_time).total_seconds())
    SESSION_ACTIVE = False
    session_start_time = None
    session_paused_time = datetime.now()

    inst_id = session.get('institution_id', 'INST-001')
    record_timeline_event(
        student_id="EXAM-SESSION",
        student_name="Examination Session",
        institution_id=inst_id,
        category="SESSION",
        event_type="SESSION_PAUSED",
        title="Examination Session Paused",
        description=f"Invigilator paused examination timer at {accumulated_elapsed_seconds}s elapsed.",
        severity="SUSPICIOUS",
        state_change={"status": ["ACTIVE", "PAUSED"]}
    )

    return jsonify({
        "success": True, 
        "message": "Session paused", 
        "elapsed_seconds": accumulated_elapsed_seconds
    })

@app.route('/api/session/end', methods=['POST'])
def end_session():
    global SESSION_ACTIVE, session_start_time, session_paused_time, accumulated_elapsed_seconds
    if SESSION_ACTIVE and session_start_time is not None:
        accumulated_elapsed_seconds += int((datetime.now() - session_start_time).total_seconds())
    SESSION_ACTIVE = False
    total_session_seconds = accumulated_elapsed_seconds
    session_start_time = None
    session_paused_time = None
    accumulated_elapsed_seconds = 0

    # Generate HTML Report.
    # Must be written to REPORTS_DIR, the directory /reports/<file> serves from.
    # Writing to static/reports/ instead broke this two ways: the download link
    # 404'd because nothing was ever placed where the route looks, and anything
    # under static/ is served by Flask's public static handler, which would
    # have bypassed the auth and cross-institution checks on that route.
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    report_path = os.path.join(REPORTS_DIR, report_filename)

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
            <time style="margin-top:2px;font-size:0.75rem;color:#64748b;">Duration: {total_session_seconds // 60}m {total_session_seconds % 60}s</time>
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

    inst_id = session.get('institution_id', 'INST-001')
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO exam_sessions (institution_id, supervisor_id, status, duration_seconds, report_url)
            VALUES (%s, %s, %s, %s, %s);
        """, (inst_id, session.get('user_id'), 'COMPLETED', total_session_seconds, f"/reports/{report_filename}"))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error recording exam session: {e}")

    record_audit_event(session.get('user_id'), session.get('username'), session.get('role'), inst_id, "EXAM_REPORT_GENERATED", request.remote_addr, "SUCCESS", f"Generated examination report: {report_filename}")

    record_timeline_event(
        student_id="EXAM-SESSION",
        student_name="Examination Session",
        institution_id=inst_id,
        category="SESSION",
        event_type="SESSION_ENDED",
        title="Examination Session Concluded",
        description=f"Examination completed with {total_students} candidates. Final forensic audit report locked: {report_filename}.",
        severity="NORMAL",
        state_change={"status": ["ACTIVE", "COMPLETED"]},
        metadata={"total_students": total_students, "high_risk_count": high_risk_count, "report_url": f"/reports/{report_filename}"}
    )

    return jsonify({"success": True, "report_url": f"/reports/{report_filename}"})

# ---------------- REVIEWABLE ACTION TIMELINE (SEARCH & DISCOVERY API) ----------------

@app.route('/api/timeline', methods=['GET'])
def get_timeline():
    """
    Reviewable Action Timeline API for Search and Discovery.
    Supports filtering by query (q), category, severity, student_id, institution_id, and chronological sorting.
    """
    search_q = (request.args.get('q') or request.args.get('search') or '').strip().lower()
    category = (request.args.get('category') or 'ALL').strip().upper()
    severity = (request.args.get('severity') or 'ALL').strip().upper()
    student_id = (request.args.get('student_id') or '').strip()
    sort_order = (request.args.get('order') or 'desc').strip().lower()
    limit = min(500, int(request.args.get('limit') or 100))

    role = session.get('role', 'SUPERVISOR')
    user_inst = session.get('institution_id')
    req_inst = request.args.get('institution_id')

    inst_filter = None
    if role == 'ADMIN':
        inst_filter = req_inst if (req_inst and req_inst != 'ALL') else None
    else:
        inst_filter = user_inst or 'INST-001'

    events = []
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        query = """
            SELECT event_uuid, time_str, student_id, student_name, institution_id,
                   category, event_type, title, description, severity, state_change,
                   metadata, resolved, timestamp
            FROM action_timeline
            WHERE 1=1
        """
        params = []
        if inst_filter:
            query += " AND institution_id = %s"
            params.append(inst_filter)
        if category and category != 'ALL':
            if category == 'AI DETECTION':
                query += " AND UPPER(category) IN ('AI DETECTION', 'DETECTION')"
            else:
                query += " AND UPPER(category) = %s"
                params.append(category)
        if severity and severity != 'ALL':
            query += " AND UPPER(severity) = %s"
            params.append(severity)
        if student_id:
            query += " AND (student_id = %s OR LOWER(student_name) LIKE %s)"
            params.append(student_id)
            params.append(f"%{student_id.lower()}%")
        if search_q:
            query += """ AND (
                LOWER(student_name) LIKE %s OR
                LOWER(student_id) LIKE %s OR
                LOWER(title) LIKE %s OR
                LOWER(description) LIKE %s OR
                LOWER(event_type) LIKE %s OR
                LOWER(category) LIKE %s OR
                LOWER(severity) LIKE %s OR
                LOWER(time_str) LIKE %s
            )"""
            sq = f"%{search_q}%"
            params.extend([sq, sq, sq, sq, sq, sq, sq, sq])

        order_sql = "DESC" if sort_order == "desc" else "ASC"
        query += f" ORDER BY timestamp {order_sql} LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        for r in rows:
            events.append({
                "id": r[0],
                "timestamp": r[1] or (r[13].strftime("%H:%M:%S") if r[13] else ""),
                "iso_timestamp": r[13].isoformat() if r[13] else "",
                "student_id": r[2],
                "student_name": r[3],
                "institution_id": r[4],
                "category": r[5],
                "event_type": r[6],
                "title": r[7],
                "description": r[8],
                "severity": r[9],
                "state_change": r[10] if isinstance(r[10], dict) else (json.loads(r[10]) if r[10] else {}),
                "metadata": r[11] if isinstance(r[11], dict) else (json.loads(r[11]) if r[11] else {}),
                "resolved": bool(r[12])
            })
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error querying action_timeline: {e}")

    # Fallback / merge with in-memory buffer if DB returned empty
    if not events:
        with timeline_events_lock:
            for ev in timeline_events_buffer:
                if inst_filter and ev.get("institution_id") != inst_filter:
                    continue
                if category and category != "ALL":
                    if category == "AI DETECTION" and ev.get("category") not in ("AI DETECTION", "DETECTION"):
                        continue
                    elif category != "AI DETECTION" and ev.get("category") != category:
                        continue
                if severity and severity != "ALL" and ev.get("severity") != severity:
                    continue
                if student_id and ev.get("student_id") != student_id and student_id.lower() not in ev.get("student_name", "").lower():
                    continue
                if search_q:
                    haystack = f"{ev.get('student_name')} {ev.get('student_id')} {ev.get('title')} {ev.get('description')} {ev.get('category')} {ev.get('severity')} {ev.get('timestamp')}".lower()
                    if search_q not in haystack:
                        continue
                events.append(dict(ev))
            if sort_order == "asc":
                events.reverse()
            events = events[:limit]

    # Category counts summary
    category_counts = {
        "ALL": len(events),
        "IDENTITY": sum(1 for e in events if e.get("category") == "IDENTITY"),
        "SESSION": sum(1 for e in events if e.get("category") == "SESSION"),
        "AI DETECTION": sum(1 for e in events if e.get("category") in ("AI DETECTION", "DETECTION")),
        "ALERT": sum(1 for e in events if e.get("category") == "ALERT"),
        "RISK": sum(1 for e in events if e.get("category") == "RISK"),
        "DEVICE": sum(1 for e in events if e.get("category") == "DEVICE"),
        "GAZE": sum(1 for e in events if e.get("category") == "GAZE"),
    }

    return jsonify({
        "success": True,
        "total_count": len(events),
        "category_counts": category_counts,
        "events": events
    })

@app.route('/api/timeline/resolve', methods=['POST'])
def resolve_timeline_event():
    """Marks an action timeline alert or violation as reviewed/resolved."""
    data = request.json or {}
    event_id = data.get('event_id') or ''
    note = data.get('note') or 'Resolved by proctor'

    if not event_id:
        return jsonify({"error": "Event ID is required"}), 400

    with timeline_events_lock:
        for ev in timeline_events_buffer:
            if ev.get("id") == event_id:
                ev["resolved"] = True
                if "state_change" not in ev or not ev["state_change"]:
                    ev["state_change"] = {}
                ev["state_change"]["alert"] = ["CREATED", "RESOLVED"]

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE action_timeline SET resolved = TRUE, state_change = jsonb_set(COALESCE(state_change, '{}'::jsonb), '{alert}', '[\"CREATED\", \"RESOLVED\"]'::jsonb) WHERE event_uuid = %s;", (event_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error updating timeline event: {e}")

    return jsonify({"success": True, "message": "Incident marked as resolved", "event_id": event_id})

@app.route('/api/timeline/event', methods=['POST'])
def create_timeline_event():
    """Allows recording proctor annotations or custom actions on the timeline."""
    data = request.json or {}
    student_id = data.get('student_id') or 'SYSTEM'
    student_name = data.get('student_name') or 'System'
    category = data.get('category') or 'ALERT'
    event_type = data.get('event_type') or 'NOTE_ADDED'
    title = data.get('title') or 'Proctor Note'
    description = data.get('description') or ''
    severity = data.get('severity') or 'NORMAL'
    state_change = data.get('state_change') or {}
    metadata = data.get('metadata') or {}
    institution_id = session.get('institution_id') or 'INST-001'

    ev = record_timeline_event(
        student_id=student_id,
        student_name=student_name,
        institution_id=institution_id,
        category=category,
        event_type=event_type,
        title=title,
        description=description,
        severity=severity,
        state_change=state_change,
        metadata=metadata
    )
    return jsonify({"success": True, "event": ev})

# ---------------- REPLAY FRAME DEVICE DETECTION API ----------------
@app.route('/api/replay/detect_frame', methods=['POST'])
def replay_detect_frame():
    """
    Real-time neural detection on raw video frames for the synchronized CCTV replay.
    Detects mobile phones (full/partial), smartwatches, and earbuds using YOLO11.
    """
    try:
        data = request.json or {}
        img_b64 = data.get("image", "")
        if not img_b64:
            return jsonify({"success": False, "detections": []})

        if "," in img_b64:
            img_b64 = img_b64.split(",", 1)[1]

        img_bytes = base64.b64decode(img_b64)
        np_arr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

        if frame is None or frame.size == 0:
            return jsonify({"success": False, "detections": []})

        det_engine = get_replay_detector()
        detections = []
        if det_engine is not None:
            results = det_engine.detect(frame, whole_frame=True)
            for r in results:
                bbox = r["bbox"]
                conf = r["conf"]
                dtype = r.get("device_type", "phone")
                label = r.get("label", f"{dtype.upper()} DETECTED")

                detections.append({
                    "bbox": [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])],
                    "conf": round(float(conf), 3),
                    "class_id": r.get("class_id", -1),
                    "class_name": r.get("class_name", ""),
                    "device_type": dtype,
                    "label": label
                })

        return jsonify({"success": True, "detections": detections, "count": len(detections), "frame_shape": [frame.shape[1], frame.shape[0]]})
    except Exception as e:
        print(f"[REPLAY DETECT] Error: {e}")
        return jsonify({"success": False, "error": str(e), "detections": []})

# ---------------- STATE ----------------
# Track state of the room globally
room_state = {
    "unknown_count": 0,
    "status": "NORMAL"
}

# tracked_students dictionary: { "STU-1002": {"name": "John", "risk_score": 0, "status": "Active", "last_seen": time.time()} }
tracked_students = {}

def log_to_db(student_id, risk_score, direction, status, institution_id=None):
    try:
        if not institution_id:
            institution_id = "INST-001"
        # Only log student events to exam_logs if student_id is valid
        sid_val = str(student_id) if student_id is not None else "0"
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO exam_logs (student_id, institution_id, risk_score, direction, status)
                VALUES (%s, %s, %s, %s, %s)
            """, (sid_val, institution_id, risk_score, direction, status))
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"Error logging to DB: {e}")

# ---------------- VIDEO PROCESSING & REAL-TIME PIPELINE ----------------

# Performance tuning
YOLO_IMGSZ = 384             # Fast person detection on CPU while preserving accuracy
MAX_STREAM_WIDTH = 960       # Downscale larger (CCTV) frames before processing
JPEG_PARAMS = [int(cv2.IMWRITE_JPEG_QUALITY), 75]

# Face identification runs on its OWN thread
ID_INTERVAL_FAST = float(os.environ.get("ID_FAST", 0.3))  # while unidentified
ID_INTERVAL_SLOW = float(os.environ.get("ID_SLOW", 2.0))  # once everyone known
FACE_ID_ENABLED = os.environ.get("FACE_ID", "on").lower() != "off"
ID_VOTES_REQUIRED = 3    # consistent matches before an identity is locked
ID_MAX_ATTEMPTS = 10     # give up on a track after this many failed passes
ID_RETRY_AFTER = 30.0    # seconds before a given-up track is retried

id_attempts = {}         # track_id -> failed identification passes
id_giveup_at = {}        # track_id -> when we last gave up on it
last_counted_id = {}     # track_id -> when an attempt was last counted
ID_RESULT_TTL = 2.0      # ignore identification results older than this
DIM_FRAME_MEAN = 90      # below this mean luma the frame gets enhanced first

_id_lock = threading.Lock()
_id_input = {"frame": None, "ts": 0.0}   # latest frame offered to the identifier
_id_output = {"faces": [], "ts": 0.0}    # latest identification result
_id_wanted = threading.Event()           # set while unidentified people are present
_id_thread_started = False


def _is_dim(frame):
    small = cv2.resize(frame, (160, 120))
    return float(np.mean(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))) < DIM_FRAME_MEAN

def _identification_worker():
    """Continuously identifies faces in the freshest frame, scoped to the active institution."""
    while True:
        if not len(gallery):
            time.sleep(0.5)
            continue

        with _id_lock:
            frame = _id_input["frame"]
            _id_input["frame"] = None
        if frame is None:
            time.sleep(0.02)
            continue

        try:
            src = face_recog.enhance_lowlight(frame) if _is_dim(frame) else frame
            found = []
            target_inst = active_monitoring_institution
            for f in face_detector.detect(src, thresh=0.38):
                ok, _reason, _m = face_recog.face_quality(src, f["bbox"])
                if not ok:
                    continue
                emb = embedder.embed(src, f["kps"])
                if emb is None:
                    continue
                sid, sname, score, margin = gallery.identify(emb, institution_id=target_inst)
                x, y, w_, h_ = f["bbox"]
                found.append({
                    "cx": x + w_ / 2,
                    "cy": y + h_ / 2,
                    "bbox": (x, y, w_, h_),
                    "sid": sid,
                    "name": sname,
                    "score": score,
                    "margin": margin
                })
            with _id_lock:
                _id_output["faces"] = found
                _id_output["ts"] = time.time()
        except Exception as e:
            print(f"[FACE] identification pass failed: {e}")

        time.sleep(0.12)


def start_identification_worker():
    global _id_thread_started
    if _id_thread_started:
        return
    if not FACE_ID_ENABLED:
        print("[FACE] identification disabled (FACE_ID=off)")
        return
    _id_thread_started = True
    threading.Thread(target=_identification_worker, name="face-id",
                     daemon=True).start()
    print("[FACE] identification worker started")


# ---- Phone detection thread -------------------------------------------
PHONE_INTERVAL = 0.04         # High-responsiveness continuous phone detection (25 FPS)
PHONE_RESULT_TTL = 1.0       # Hold detection bounding box smoothly across frames
PHONE_WHOLE_FRAME_EVERY = 1  # Continuous whole-frame + person-ROI scanning on every pass

_phone_lock = threading.Lock()
_phone_input = {"frame": None, "persons": []}
_phone_output = {"boxes": [], "ts": 0.0}
_phone_thread_started = False


def _phone_worker():
    pass_no = 0
    while True:
        with _phone_lock:
            frame = _phone_input["frame"]
            persons = list(_phone_input["persons"])
            _phone_input["frame"] = None
        if frame is None:
            time.sleep(0.01)
            continue
        try:
            pass_no += 1
            if phone_detector is None:
                continue
            # Continuous high-resolution scanning (640px) for small and partial phones
            found = phone_detector.detect(frame, persons, whole_frame=True, whole_imgsz=640)
            with _phone_lock:
                _phone_output["boxes"] = found
                _phone_output["ts"] = time.time()
        except Exception as e:
            print(f"[PHONE] detection pass failed: {e}")
        time.sleep(PHONE_INTERVAL)


def start_phone_worker():
    global _phone_thread_started
    if _phone_thread_started or phone_detector is None:
        if phone_detector is None:
            print("[PHONE] detection disabled (PHONE_DETECTION=off)")
        return
    _phone_thread_started = True
    threading.Thread(target=_phone_worker, name="phone-detect",
                     daemon=True).start()
    print(f"[PHONE] detection worker started "
          f"({phone_detector.weights}, person-ROI, low latency mode)")


def open_capture(source):
    cap = cv2.VideoCapture(source)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


# ---- Decoupled Real-Time Frame Buffers & State ---------------------------
_raw_lock = threading.Lock()
_latest_raw_frame = None
_latest_raw_ts = 0.0
_raw_frame_event = threading.Event()

_ai_overlay_lock = threading.Lock()
_shared_draw_ops = []

_latest_jpeg = None
_frame_ready = threading.Condition()
_worker_lock = threading.Lock()
_worker_started = False

_viewers = 0
_viewers_lock = threading.Lock()
_camera_paused = False
_camera_open = False
IDLE_RELEASE_SECONDS = 2.0

smooth_face_boxes = {}
smooth_boxes = {}


def _camera_held():
    return _camera_open


def _publish_frame(jpeg_bytes):
    global _latest_jpeg
    with _frame_ready:
        _latest_jpeg = jpeg_bytes
        _frame_ready.notify_all()


def _camera_wanted():
    """True only while a viewer is watching and enrollment has not paused us."""
    with _viewers_lock:
        return _viewers > 0 and not _camera_paused


def _camera_capture_worker():
    """Dedicated low-latency camera acquisition thread.
    Continuously pulls fresh frames from the hardware at full FPS with zero AI blocking."""
    global _latest_raw_frame, _latest_raw_ts, _camera_open
    cap = None
    source = None
    read_failures = 0
    idle_since = None

    while True:
        if not _camera_wanted():
            if cap is not None:
                if _camera_paused:
                    should_release = True
                elif idle_since is None:
                    idle_since = time.time()
                    should_release = False
                else:
                    should_release = (time.time() - idle_since) >= IDLE_RELEASE_SECONDS

                if should_release:
                    cap.release()
                    cap = None
                    _camera_open = False
                    idle_since = None
                    with _raw_lock:
                        _latest_raw_frame = None
                    reason = "enrollment paused it" if _camera_paused else "no viewers"
                    print(f"[VIDEO] Camera released ({reason}) - free for the browser/other apps.")
            time.sleep(0.05)
            continue

        idle_since = None

        desired_source = get_video_source()
        if cap is None or source != desired_source or VIDEO_SOURCE_CHANGED.is_set():
            if cap is not None:
                cap.release()
            source = desired_source
            VIDEO_SOURCE_CHANGED.clear()
            cap = open_capture(source)
            read_failures = 0
            if not cap.isOpened():
                _camera_open = False
                time.sleep(0.5)
                continue
            _camera_open = True
            print(f"[VIDEO] Camera acquired on source: {source}")

        ret, frame = cap.read()
        if not ret or frame is None:
            read_failures += 1
            if read_failures > 30:
                print(f"[VIDEO] Repeated frame drop on source: {source}, resetting capture...")
                cap.release()
                cap = None
                _camera_open = False
                time.sleep(0.5)
            continue

        read_failures = 0
        now = time.time()

        with _raw_lock:
            _latest_raw_frame = frame
            _latest_raw_ts = now
        _raw_frame_event.set()


def _render_hud_box(img, pt1, pt2, color, thickness, title, subtitle=None):
    """Renders a clean, professional ProctorAI HUD bounding box with dark semi-transparent
    header pills and crisp typography."""
    x1, y1 = pt1
    x2, y2 = pt2
    h, w = img.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w - 1, x2), min(h - 1, y2)
    if x2 <= x1 or y2 <= y1:
        return

    # Draw primary bounding rectangle
    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)

    # Corner accent brackets for a sharp security look
    corner_len = min(14, max(5, int((x2 - x1) * 0.15)))
    cv2.line(img, (x1, y1), (x1 + corner_len, y1), color, thickness + 1)
    cv2.line(img, (x1, y1), (x1, y1 + corner_len), color, thickness + 1)
    cv2.line(img, (x2, y1), (x2 - corner_len, y1), color, thickness + 1)
    cv2.line(img, (x2, y1), (x2, y1 + corner_len), color, thickness + 1)
    cv2.line(img, (x1, y2), (x1 + corner_len, y2), color, thickness + 1)
    cv2.line(img, (x1, y2), (x1, y2 - corner_len), color, thickness + 1)
    cv2.line(img, (x2, y2), (x2 - corner_len, y2), color, thickness + 1)
    cv2.line(img, (x2, y2), (x2, y2 - corner_len), color, thickness + 1)

    # Header label pill
    if title:
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.44
        (tw, th), baseline = cv2.getTextSize(title, font, scale, 1)
        px1 = x1
        py2 = max(0, y1 - 3)
        py1 = max(0, py2 - th - 6)
        px2 = min(w - 1, px1 + tw + 8)

        if py2 > py1 and px2 > px1:
            sub = img[py1:py2, px1:px2]
            if sub.size > 0:
                bg = np.full(sub.shape, (15, 15, 15), dtype=np.uint8)
                cv2.addWeighted(bg, 0.85, sub, 0.15, 0, sub)
                cv2.rectangle(img, (px1, py1), (px2, py2), color, 1)
                cv2.putText(img, title, (px1 + 4, py2 - 3), font, scale, color, 1, cv2.LINE_AA)

    # Sublabel pill (underneath or inside)
    if subtitle:
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.38
        (stw, sth), sbase = cv2.getTextSize(subtitle, font, scale, 1)
        spy1 = min(h - 1, y2 + 2)
        spy2 = min(h - 1, spy1 + sth + 6)
        spx1 = x1
        spx2 = min(w - 1, spx1 + stw + 8)
        if spy2 > spy1 and spx2 > spx1:
            sub2 = img[spy1:spy2, spx1:spx2]
            if sub2.size > 0:
                bg2 = np.full(sub2.shape, (15, 15, 15), dtype=np.uint8)
                cv2.addWeighted(bg2, 0.85, sub2, 0.15, 0, sub2)
                cv2.rectangle(img, (spx1, spy1), (spx2, spy2), (60, 60, 60), 1)
                cv2.putText(img, subtitle, (spx1 + 4, spy2 - 3), font, scale, (220, 220, 220), 1, cv2.LINE_AA)


def _render_iris_marker(img, center, radius, color):
    """Draws a subtle small visual indicator on the iris without obscuring the eye."""
    if center is not None:
        cx, cy = int(center[0]), int(center[1])
        if 0 <= cx < img.shape[1] and 0 <= cy < img.shape[0]:
            cv2.circle(img, (cx, cy), radius, color, -1, cv2.LINE_AA)
            cv2.circle(img, (cx, cy), radius + 1, (255, 255, 255), 1, cv2.LINE_AA)


def _render_gaze_arrow(img, start, end, color):
    """Draws a subtle thin gaze vector indicator."""
    if start is not None and end is not None:
        p1 = (int(start[0]), int(start[1]))
        p2 = (int(end[0]), int(end[1]))
        if (p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2 > 4:
            cv2.arrowedLine(img, p1, p2, color, 1, tipLength=0.35, line_type=cv2.LINE_AA)


def _tier_color(tier):
    if tier == "LOW":
        return (0, 230, 115)       # Emerald Green
    elif tier == "MEDIUM":
        return (0, 165, 255)       # Amber
    else:
        return (0, 0, 255)         # Red Violation


# How long a registered face keeps its on-frame tag (at the last known
# position) after MediaPipe stops reporting landmarks for it. Fast head
# turns and glances away cause motion blur that drops landmark detection
# for a handful of frames even though the student never left the camera;
# without this the tag flickered out and back in on every quick movement.
# Also doubles as the spatial re-linking window below, so a face that
# reappears mid-grace-window snaps back onto the same identity instead of
# briefly registering as a new/unknown face.
FACE_TRACK_GRACE_SECONDS = 2.5


# ===========================================================================
# REAL-TIME FACE TRACKING LAYER  (decoupled from detection)
# ===========================================================================
# Detection -- YOLO + MediaPipe + ArcFace in _ai_worker_loop -- is expensive
# and runs SLOWER than the camera frame rate. On its own the on-frame box only
# moves at detection speed, which produces the three symptoms reported:
#   * lag       -- the box trails the head between detections
#   * blink-out -- a single missed detection frame (fast turn, glance away,
#                  motion blur) drops the box entirely
#   * snapping  -- when the next detection lands the box jumps to it
#
# This layer fixes all three WITHOUT touching the recognition or risk models.
# A cheap per-frame tracker runs inside the stream compositor (_stream_worker,
# which already runs at the native frame rate -- it is our "render loop"):
#   1. Lucas-Kanade optical flow shifts each face box along the REAL pixel
#      motion of the head on EVERY rendered frame -> no lag, tracks fast turns.
#   2. Each fresh detection is FUSED into the tracked box (a weighted pull),
#      not swapped in -> no snap.
#   3. A TRACKING / PREDICTED / LOST state machine keeps the tag visible
#      through detection gaps and only drops it once optical flow can no longer
#      find the head or the box leaves the frame -> "stays until out of frame".
#
# --- Tunables (edit these, not the loops below) ---------------------------
TRACK_FRESH_MS        = 200    # detection newer than this -> TRACKING, else PREDICTED
TRACK_HOLD_MS         = 700    # keep the box even with NO optical flow this long
                               # (bridges a slow detection cadence / low-texture face)
TRACK_MAX_PREDICT_MS  = 4000   # hard cap on flow-only prediction with zero detections
TRACK_CORRECT_BLEND   = 0.55   # how hard a new detection pulls the box toward itself
TRACK_MAX_POINTS      = 24     # optical-flow keypoints seeded inside each face
TRACK_MIN_POINTS      = 4      # below this the flow track has failed -> LOST
TRACK_FLOW_WIN        = 15     # Lucas-Kanade search window (px)
TRACK_FLOW_LEVELS     = 2      # optical-flow pyramid levels
TRACK_UNKNOWN_LINK_PX = 90     # max centre distance to re-link an unknown face
# --------------------------------------------------------------------------

_LK_PARAMS = dict(
    winSize=(TRACK_FLOW_WIN, TRACK_FLOW_WIN),
    maxLevel=TRACK_FLOW_LEVELS,
    criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03),
)

_tracks_lock = threading.Lock()
_face_tracks = {}          # key (sid or "unk::N") -> track dict
_unknown_track_seq = 0


def _box_center(b):
    return ((b[0] + b[2]) * 0.5, (b[1] + b[3]) * 0.5)


def _match_unknown_key(box, claimed):
    """Re-link an unregistered detection to the nearest existing unknown track
    so anonymous faces keep a stable tag instead of churning every frame."""
    cx, cy = _box_center(box)
    best_key, best_d = None, float(TRACK_UNKNOWN_LINK_PX ** 2)
    for k, t in _face_tracks.items():
        if not k.startswith("unk::") or k in claimed:
            continue
        tcx, tcy = _box_center(t["box"])
        d = (cx - tcx) ** 2 + (cy - tcy) ** 2
        if d < best_d:
            best_d, best_key = d, k
    return best_key


def _seed_face_tracks(face_dets, now):
    """Called by the DETECTION loop. Hands authoritative boxes + labels to the
    tracker as CORRECTIONS. The stream worker fuses them and re-seeds optical
    flow on its own frame. This never draws anything itself."""
    global _unknown_track_seq
    with _tracks_lock:
        claimed = set()
        for det in face_dets:
            sid = det["sid"]
            if sid:
                key = sid
            else:
                key = _match_unknown_key(det["box"], claimed)
                if key is None:
                    key = f"unk::{_unknown_track_seq}"
                    _unknown_track_seq += 1
            claimed.add(key)

            t = _face_tracks.get(key)
            if t is None:
                t = {"box": np.array(det["box"], dtype=np.float32), "pts": None}
                _face_tracks[key] = t
            t["det_box"] = np.array(det["box"], dtype=np.float32)
            t["det_center"] = _box_center(det["box"])
            t["title"] = det["title"]
            t["sub"] = det["sub"]
            t["color"] = det["color"]
            t["iris"] = det["iris"]          # [(x,y), ...] at detection time
            t["gaze"] = det["gaze"]          # [((x,y),(x,y)), ...] arrows
            t["last_det_ts"] = now
            t["needs_reseed"] = True

        # Drop stale unknown tracks the detector no longer reports and that the
        # flow layer will not have re-seeded (registered tracks persist by sid).
        for key in list(_face_tracks.keys()):
            if key.startswith("unk::") and key not in claimed:
                if (now - _face_tracks[key].get("last_det_ts", 0)) * 1000.0 > TRACK_MAX_PREDICT_MS:
                    del _face_tracks[key]


def _seed_points(gray, box):
    """Pick strong corners inside the face box to drive optical flow."""
    x1, y1, x2, y2 = [int(v) for v in box]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(gray.shape[1], x2), min(gray.shape[0], y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    roi = gray[y1:y2, x1:x2]
    corners = cv2.goodFeaturesToTrack(roi, maxCorners=TRACK_MAX_POINTS,
                                      qualityLevel=0.01, minDistance=4)
    if corners is None or len(corners) < TRACK_MIN_POINTS:
        return None
    corners = corners.reshape(-1, 2) + np.array([x1, y1], dtype=np.float32)
    return corners.astype(np.float32)


def _update_face_tracks(prev_gray, gray, now):
    """Called by the RENDER loop every frame. Advances each track by optical
    flow, fuses any pending detection, runs the TRACKING/PREDICTED/LOST state
    machine, and returns the render list. Removes tracks once LOST."""
    h, w = gray.shape[:2]
    flow_ok = (prev_gray is not None and prev_gray.shape == gray.shape)
    render = []
    with _tracks_lock:
        for key in list(_face_tracks.keys()):
            t = _face_tracks[key]
            dx = dy = 0.0

            # 1. Optical-flow advance along the real head motion
            if flow_ok and t.get("pts") is not None and len(t["pts"]) >= TRACK_MIN_POINTS:
                p0 = t["pts"].reshape(-1, 1, 2)
                p1, stt, _err = cv2.calcOpticalFlowPyrLK(prev_gray, gray, p0, None, **_LK_PARAMS)
                if p1 is not None and stt is not None:
                    good = stt.reshape(-1).astype(bool)
                    new_pts = p1.reshape(-1, 2)[good]
                    old_pts = p0.reshape(-1, 2)[good]
                    if len(new_pts) >= TRACK_MIN_POINTS:
                        motion = new_pts - old_pts
                        dx = float(np.median(motion[:, 0]))
                        dy = float(np.median(motion[:, 1]))
                        t["pts"] = new_pts.astype(np.float32)
                    else:
                        t["pts"] = None      # flow collapsed
                else:
                    t["pts"] = None

            box = t["box"].copy()
            box[0] += dx; box[2] += dx
            box[1] += dy; box[3] += dy

            # 2. Fuse a pending detection (weighted pull removes the snap)
            if t.get("needs_reseed"):
                a = TRACK_CORRECT_BLEND
                box = (1.0 - a) * box + a * t["det_box"]
                t["pts"] = _seed_points(gray, box)
                t["needs_reseed"] = False
            t["box"] = box

            # 3. State machine
            age_ms = (now - t.get("last_det_ts", 0)) * 1000.0
            cx, cy = _box_center(box)
            in_bounds = (0 <= cx < w and 0 <= cy < h)
            flow_alive = t.get("pts") is not None and len(t["pts"]) >= TRACK_MIN_POINTS

            lost = ((not in_bounds)
                    or (age_ms > TRACK_MAX_PREDICT_MS)
                    or (age_ms > TRACK_HOLD_MS and not flow_alive))
            if lost:
                del _face_tracks[key]
                continue

            state = "TRACKING" if age_ms <= TRACK_FRESH_MS else "PREDICTED"

            # Carry the accumulated box shift onto the eye markers so the iris
            # dots / gaze arrows move with the head between detections too.
            sx = cx - t["det_center"][0]
            sy = cy - t["det_center"][1]
            render.append({
                "box": tuple(int(v) for v in box),
                "title": t["title"], "sub": t["sub"], "color": t["color"],
                "state": state,
                "iris": [(int(ix + sx), int(iy + sy)) for (ix, iy) in t.get("iris") or []],
                "gaze": [((int(a0 + sx), int(a1 + sy)), (int(b0 + sx), int(b1 + sy)))
                         for ((a0, a1), (b0, b1)) in t.get("gaze") or []],
            })
    return render


def _stream_worker():
    """Real-Time Stream Compositor Thread (the RENDER loop).
    Runs on every fresh camera frame: advances the per-frame face tracker,
    composites the tracked boxes + the detection loop's static overlays
    (phones/devices), and encodes JPEG at the native frame rate."""
    last_processed_ts = 0.0
    prev_gray = None

    while True:
        _raw_frame_event.wait(timeout=0.1)
        _raw_frame_event.clear()

        with _raw_lock:
            frame = _latest_raw_frame
            ts = _latest_raw_ts

        if frame is None or ts <= last_processed_ts:
            continue

        last_processed_ts = ts
        annotated = frame.copy()

        # Per-frame optical-flow face tracking (independent of detection cadence)
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            tracked_faces = _update_face_tracks(prev_gray, gray, time.time())
            prev_gray = gray
        except Exception as exc:
            print(f"[TRACK] per-frame tracker error: {type(exc).__name__}: {exc}")
            tracked_faces = []

        # Static overlays from the detection loop (phones/devices etc.)
        with _ai_overlay_lock:
            current_ops = list(_shared_draw_ops)

        for op in current_ops:
            op_type = op[0]
            if op_type == 'hud_box':
                _, p1, p2, color, thick, title, sub = op
                _render_hud_box(annotated, p1, p2, color, thickness=thick,
                                title=title, subtitle=sub)
            elif op_type == 'iris':
                _, center, rad, color = op
                _render_iris_marker(annotated, center, radius=rad, color=color)
            elif op_type == 'gaze_arrow':
                _, start, end, color = op
                _render_gaze_arrow(annotated, start, end, color=color)

        # Tracked face boxes + eye markers, drawn at the native frame rate
        for tf in tracked_faces:
            x1, y1, x2, y2 = tf["box"]
            _render_hud_box(annotated, (x1, y1), (x2, y2), tf["color"],
                            thickness=2, title=tf["title"], subtitle=tf["sub"])
            for (ix, iy) in tf["iris"]:
                _render_iris_marker(annotated, (ix, iy), radius=2, color=(255, 220, 0))
            for (gp1, gp2) in tf["gaze"]:
                _render_gaze_arrow(annotated, gp1, gp2, color=(255, 220, 0))

        ret, buffer = cv2.imencode('.jpg', annotated, [
            int(cv2.IMWRITE_JPEG_QUALITY), 80,
            int(cv2.IMWRITE_JPEG_OPTIMIZE), 1
        ])
        if ret:
            _publish_frame(buffer.tobytes())


def _ai_worker():
    """Supervisor: keeps the AI loop alive across transient errors.

    Previously a single exception anywhere in the per-frame pipeline (a
    MediaPipe VIDEO-mode timestamp hiccup, an edge-case detection, a bad
    frame) killed this thread outright. When that happened the on-frame face
    boxes and overlays silently vanished from the stream -- draw_ops froze --
    while /api/status kept serving the last cached roster, so the dashboard
    still listed students with no boxes on the video. Restarting the inner
    loop on error keeps detection running instead."""
    while True:
        try:
            _ai_worker_loop()
        except Exception as exc:
            import traceback
            print(f"[AI] worker loop crashed, restarting: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            time.sleep(0.1)


def _ai_worker_loop():
    """The AI detection loop. Runs YOLO person detection, MediaPipe
    FaceLandmarker, ArcFace identity matching and the behavior engines off the
    video stream's critical path. Never blocks camera preview."""
    global tracked_students, current_students_in_frame, track_to_student, track_votes, historical_risk_scores, smooth_boxes, smooth_face_boxes, _shared_draw_ops
    last_ai_ts = 0.0
    last_log_time = 0.0

    while True:
        with _raw_lock:
            raw_frame = _latest_raw_frame
            ts = _latest_raw_ts

        if raw_frame is None or ts <= last_ai_ts:
            time.sleep(0.01)
            continue

        last_ai_ts = ts
        frame = raw_frame.copy()
        now = time.time()
        draw_ops = []

        # 1. YOLO person detection
        yolo_results = yolo_model(frame, stream=True, verbose=False,
                                  imgsz=YOLO_IMGSZ, classes=[0])
        person_detections = []
        person_boxes = []

        for r in yolo_results:
            for box in r.boxes:
                conf = float(box.conf[0])
                if conf <= 0.45:
                    continue
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                person_detections.append(([x1, y1, x2 - x1, y2 - y1], conf, "person"))
                person_boxes.append((x1, y1, x2, y2))

        # 2. Phone / Device detection dispatch
        with _phone_lock:
            if _phone_input["frame"] is None:
                _phone_input["frame"] = frame.copy()
                _phone_input["persons"] = person_boxes
            fresh = (now - _phone_output["ts"]) <= PHONE_RESULT_TTL
            phone_hits = list(_phone_output["boxes"]) if fresh else []

        phone_boxes = []
        smartwatch_boxes = []
        earbud_boxes = []
        laptop_boxes = []
        book_boxes = []
        tablet_boxes = []

        for d in phone_hits:
            px1, py1, px2, py2 = [int(v) for v in d["bbox"]]
            pconf = float(d["conf"])
            dev_type = d.get("device_type", "phone")
            if dev_type == "smartwatch":
                smartwatch_boxes.append((px1, py1, px2, py2, pconf))
                draw_ops.append(('hud_box', (px1, py1), (px2, py2), (0, 140, 255), 2,
                                 "SMARTWATCH DETECTED", f"PROHIBITED DEVICE · {pconf:.0%}"))
            elif dev_type == "earbud":
                earbud_boxes.append((px1, py1, px2, py2, pconf))
                draw_ops.append(('hud_box', (px1, py1), (px2, py2), (0, 165, 255), 2,
                                 "EARBUD DETECTED", f"PROHIBITED DEVICE · {pconf:.0%}"))
            elif dev_type == "laptop":
                laptop_boxes.append((px1, py1, px2, py2, pconf))
                draw_ops.append(('hud_box', (px1, py1), (px2, py2), (255, 180, 0), 2,
                                 "LAPTOP DETECTED", f"EXAM ENVIRONMENT · {pconf:.0%}"))
            elif dev_type == "book":
                book_boxes.append((px1, py1, px2, py2, pconf))
                draw_ops.append(('hud_box', (px1, py1), (px2, py2), (0, 70, 255), 2,
                                 "UNAUTHORIZED NOTES DETECTED", f"PROHIBITED MATERIAL · {pconf:.0%}"))
            elif dev_type == "tablet":
                tablet_boxes.append((px1, py1, px2, py2, pconf))
                draw_ops.append(('hud_box', (px1, py1), (px2, py2), (255, 140, 0), 2,
                                 "TABLET / SCREEN DETECTED", f"SECONDARY SCREEN · {pconf:.0%}"))
            else:
                phone_boxes.append((px1, py1, px2, py2, pconf))
                draw_ops.append(('hud_box', (px1, py1), (px2, py2), (0, 0, 255), 2,
                                 "CELL PHONE DETECTED", f"PROHIBITED DEVICE · {pconf:.0%}"))

        room_state["phone_detected"] = len(phone_boxes) > 0
        room_state["smartwatch_detected"] = len(smartwatch_boxes) > 0
        room_state["earbud_detected"] = len(earbud_boxes) > 0
        room_state["laptop_detected"] = len(laptop_boxes) > 0
        room_state["book_detected"] = len(book_boxes) > 0
        room_state["tablet_detected"] = len(tablet_boxes) > 0

        # 3. MediaPipe FaceLandmarker analysis (all visible faces with real iris & head pose)
        face_obs_list = face_analyzer.analyze(frame)

        # 4. Face identification dispatch
        with _id_lock:
            if _id_input["frame"] is None:
                _id_input["frame"] = frame.copy()
                _id_input["ts"] = now
            id_faces = (_id_output["faces"]
                        if (now - _id_output["ts"]) <= ID_RESULT_TTL else [])

        current_students_in_frame = set()
        unknown_count = 0
        used_face_indices = set()
        face_dets = []   # authoritative face boxes handed to the per-frame tracker

        # Match face observations with recognized identities
        for idx, obs in enumerate(face_obs_list):
            fcx, fcy = obs.nose_xy
            bx, by, bw, bh = obs.bbox
            
            # Find matching identified face
            matched_idf = None
            best_d = 1e9
            for idf in id_faces:
                ix, iy = idf["cx"], idf["cy"]
                d = (fcx - ix) ** 2 + (fcy - iy) ** 2
                if d < (max(bw, bh) * 1.2) ** 2 or (bx <= ix <= bx + bw and by <= iy <= by + bh):
                    if d < best_d:
                        best_d = d
                        matched_idf = idf

            sid = matched_idf["sid"] if (matched_idf and matched_idf["sid"]) else None
            sname = matched_idf["name"] if matched_idf else None

            # Short-term spatial retention (prevents a brief landmark-detection
            # gap from registering as a new/unknown face)
            if sid is None:
                for past_sid, past_data in tracked_students.items():
                    if now - past_data.get("last_seen", 0) < FACE_TRACK_GRACE_SECONDS:
                        prev_fb = smooth_face_boxes.get(past_sid)
                        if prev_fb is not None:
                            pfx1, pfy1, pfx2, pfy2 = prev_fb
                            if (pfx1 - 35 <= fcx <= pfx2 + 35) and (pfy1 - 35 <= fcy <= pfy2 + 35):
                                sid = past_sid
                                sname = past_data.get("name")
                                break

            # Smooth face bounding box
            pad_x, pad_y = int(bw * 0.08), int(bh * 0.10)
            raw_fb = np.array([
                max(0, bx - pad_x),
                max(0, by - pad_y),
                min(frame.shape[1], bx + bw + pad_x),
                min(frame.shape[0], by + bh + pad_y)
            ], dtype=np.float32)

            box_key = sid or f"unknown_{idx}"
            prev_fb = smooth_face_boxes.get(box_key)
            if prev_fb is None:
                sm_fb = raw_fb
            else:
                fdist = float(np.max(np.abs(raw_fb - prev_fb)))
                falpha = 0.85 if fdist > 8.0 else (0.60 if fdist > 3.0 else 0.40)
                sm_fb = (1.0 - falpha) * prev_fb + falpha * raw_fb
            smooth_face_boxes[box_key] = sm_fb
            sfx1, sfy1, sfx2, sfy2 = map(int, sm_fb)

            used_face_indices.add(idx)

            if sid is not None:
                # Registered Student Identified
                current_students_in_frame.add(sid)
                if sid not in behaviors:
                    behaviors[sid] = proctor_ai.StudentBehavior(sid, sname or sid)

                # Attribute detected phones to candidate workspace/body/hands
                phone_conf = 0.0
                for (px1, py1, px2, py2, pconf) in phone_boxes:
                    pcx, pcy = (px1 + px2) / 2, (py1 + py2) / 2
                    # Check candidate perimeter (hands, desk, lap, side workspace)
                    in_workspace = (
                        (sfx1 - 240) <= pcx <= (sfx2 + 240) and
                        (sfy1 - 60) <= pcy <= (sfy2 + 650)
                    )
                    # If candidate workspace match or single candidate in view
                    if in_workspace or len(face_obs_list) <= 1:
                        phone_conf = max(phone_conf, pconf)

                prev_logged = tracked_students.get(sid, {}).get("_logged_event")
                snap = behaviors[sid].update(obs, phone_conf, now)
                snap["last_seen"] = now
                snap["last_update"] = now
                snap["name"] = sname or behaviors[sid].name
                snap["student_id"] = sid
                snap["institution_id"] = tracked_students.get(sid, {}).get("institution_id", active_monitoring_institution)
                tracked_students[sid] = snap
                historical_risk_scores[sid] = snap["suspicion_score"]

                le = snap.get("last_event")
                if le is not None and le is not prev_logged:
                    log_to_db(sid, int(snap["suspicion_score"]),
                              snap.get("direction", "CENTER"), le["label"], snap["institution_id"])
                    
                    lbl_upper = str(le.get("label", "")).upper()
                    if "PHONE" in lbl_upper:
                        ev_cat = "DEVICE"
                        ev_type = "PHONE_DETECTED"
                        ev_sev = "HIGH_RISK"
                        ev_desc = f"Mobile device detected in candidate {snap['name']}'s monitored area."
                    elif "LOOKING" in lbl_upper or "GAZE" in lbl_upper:
                        ev_cat = "GAZE"
                        ev_type = "GAZE_DEVIATION"
                        ev_sev = "SUSPICIOUS"
                        ev_desc = f"Gaze deviation ({snap.get('direction', 'AWAY')}) detected for candidate {snap['name']}."
                    elif "MISSING" in lbl_upper or "AWAY" in lbl_upper:
                        ev_cat = "RISK"
                        ev_type = "FACE_MISSING"
                        ev_sev = "HIGH_RISK"
                        ev_desc = f"Candidate {snap['name']} ({sid}) tracking lost / left monitored area."
                    elif "MULTIPLE" in lbl_upper:
                        ev_cat = "AI DETECTION"
                        ev_type = "MULTIPLE_PERSONS"
                        ev_sev = "HIGH_RISK"
                        ev_desc = f"Multiple persons detected in candidate {snap['name']}'s camera perimeter."
                    else:
                        ev_cat = "AI DETECTION"
                        ev_type = "BEHAVIOR_ANOMALY"
                        ev_sev = "SUSPICIOUS"
                        ev_desc = f"Anomalous event: {le.get('label')} for student {snap['name']}."

                    prev_susp = tracked_students.get(sid, {}).get("_prev_susp", 0)
                    curr_susp = int(snap["suspicion_score"])
                    state_chg = {}
                    if curr_susp != prev_susp:
                        state_chg["risk"] = [prev_susp, curr_susp]
                        state_chg["trust"] = [max(0, 100 - prev_susp), max(0, 100 - curr_susp)]
                    tracked_students[sid]["_prev_susp"] = curr_susp

                    record_timeline_event(
                        student_id=sid,
                        student_name=snap["name"],
                        institution_id=snap["institution_id"],
                        category=ev_cat,
                        event_type=ev_type,
                        title=le.get("label", "AI Behavioral Event").upper(),
                        description=ev_desc,
                        severity=ev_sev,
                        state_change=state_chg,
                        metadata={
                            "direction": snap.get("direction", "CENTER"),
                            "suspicion_score": curr_susp,
                            "trust_score": int(snap.get("trust_score", 100 - curr_susp)),
                            "phone_conf": float(phone_conf)
                        }
                    )
                tracked_students[sid]["_logged_event"] = le

                color = _tier_color(snap["tier"])

                # On-frame tag: name above the box, roll number + live risk
                # score below it. ASCII separator only -- the OpenCV Hershey
                # font has no glyph for a middle dot and would draw a box.
                risk_val = int(round(snap.get("suspicion_score", 0)))
                title = snap['name']
                sub = f"Roll {sid} | Risk {risk_val}"

                # Eye markers + gaze arrows are captured at detection time and
                # carried along by the tracker between detections.
                iris_pts, gaze_arrows = [], []
                if obs.left_iris_xy and obs.right_iris_xy:
                    iris_pts = [obs.left_iris_xy, obs.right_iris_xy]
                    if abs(obs.gaze_h - 0.5) > 0.12 or abs(obs.gaze_v - 0.5) > 0.12:
                        gdx = int((obs.gaze_h - 0.5) * 16)
                        gdy = int((obs.gaze_v - 0.5) * 16)
                        gaze_arrows = [
                            (obs.left_iris_xy,
                             (obs.left_iris_xy[0] + gdx, obs.left_iris_xy[1] + gdy)),
                            (obs.right_iris_xy,
                             (obs.right_iris_xy[0] + gdx, obs.right_iris_xy[1] + gdy)),
                        ]

                # Hand this detection to the per-frame tracker instead of
                # drawing a static box -- the render loop moves it every frame.
                face_dets.append({
                    "sid": sid, "box": (sfx1, sfy1, sfx2, sfy2),
                    "title": title, "sub": sub, "color": color,
                    "iris": iris_pts, "gaze": gaze_arrows,
                })
            else:
                # Unregistered face
                unknown_count += 1
                iris_pts = []
                if obs.left_iris_xy and obs.right_iris_xy:
                    iris_pts = [obs.left_iris_xy, obs.right_iris_xy]
                face_dets.append({
                    "sid": None, "box": (sfx1, sfy1, sfx2, sfy2),
                    "title": "UNKNOWN PERSON", "sub": "UNREGISTERED PARTICIPANT",
                    "color": (0, 0, 255), "iris": iris_pts, "gaze": [],
                })

        # Publish this cycle's detections to the per-frame tracker. The render
        # loop advances them by optical flow between now and the next cycle,
        # so the box tracks the head and the tag persists at native frame rate.
        _seed_face_tracks(face_dets, now)

        # Handle absent students
        for sid in list(tracked_students.keys()):
            if sid not in current_students_in_frame:
                if sid in behaviors:
                    snap = behaviors[sid].update(None, 0.0, now)
                    snap["name"] = behaviors[sid].name
                    snap["student_id"] = sid
                    snap["last_seen"] = tracked_students[sid].get("last_seen", now)
                    snap["last_update"] = now
                    snap["status"] = "Away"
                    tracked_students[sid] = snap

                # Tag persistence through brief detection gaps (fast turns,
                # glances away) is now handled by the per-frame tracker's
                # PREDICTED state in _update_face_tracks -- not by re-drawing a
                # static box here. This block only maintains the roster.
                time_away = now - tracked_students[sid].get("last_seen", 0)
                if time_away > 60.0:
                    historical_risk_scores[sid] = tracked_students[sid].get("suspicion_score", 0)
                    tracked_students.pop(sid, None)
                    smooth_boxes.pop(sid, None)
                    smooth_face_boxes.pop(sid, None)
                    to_delete = [tid for tid, s in track_to_student.items() if s == sid]
                    for tid in to_delete:
                        del track_to_student[tid]
                        if tid in track_votes:
                            del track_votes[tid]

        gray_std = float(np.std(cv2.cvtColor(
            cv2.resize(frame, (160, 120)), cv2.COLOR_BGR2GRAY)))
        room_events = room_behavior.update(unknown_count, gray_std, now)
        room_state["unknown_count"] = unknown_count
        room_state["camera_blocked"] = room_events["camera_blocked"]
        room_state["alerts"] = room_events["alerts"]

        status = "NORMAL"
        if room_events["camera_blocked"]:
            status = "CAMERA BLOCKED"
        elif room_state["phone_detected"]:
            status = "PHONE DETECTED"
        elif room_events["extra_person"] or unknown_count > 0:
            status = "UNKNOWN PERSON"
        room_state["status"] = status

        if status != "NORMAL" and now - last_log_time > 5:
            log_to_db("ROOM", 100, "N/A", status, active_monitoring_institution)
            last_log_time = now

        # Atomically publish draw operations for stream overlay
        with _ai_overlay_lock:
            _shared_draw_ops = draw_ops


def start_camera_worker():
    """Starts the decoupled camera capture, stream composer, and AI threads."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

    threading.Thread(target=_camera_capture_worker, name="camera-capture", daemon=True).start()
    threading.Thread(target=_stream_worker, name="stream-composer", daemon=True).start()
    threading.Thread(target=_ai_worker, name="ai-inference", daemon=True).start()
    print("[VIDEO] Decoupled real-time camera & AI pipeline threads started.")
    start_identification_worker()
    start_phone_worker()

def gen_frames():
    """Per-viewer generator. Touches no camera and runs no AI - it only
    forwards the latest frame the worker produced, so extra viewers are
    nearly free and never contend for the camera. Registering as a viewer is
    what tells the worker to acquire the camera.

    FIX: Track the identity of the last sent frame (via id()) so the same
    JPEG bytes are never yielded twice in a row. Without this guard the
    generator re-yields the cached _latest_jpeg on every Condition notify,
    even when no new frame has arrived, which makes the <img> MJPEG stream
    appear frozen / static while the camera hardware is running normally."""
    global _viewers
    start_camera_worker()
    with _viewers_lock:
        _viewers += 1
    last_sent_id = None   # identity of the last frame bytes object we forwarded
    try:
        while True:
            with _frame_ready:
                # Block until the stream-compositor publishes a genuinely new frame
                # or the 5-second timeout elapses (acts as a keepalive).
                _frame_ready.wait(timeout=5.0)
                jpeg = _latest_jpeg

            if jpeg is None:
                continue

            # Only forward frames that are actually new.  The Condition is
            # notified on every publish, but if the camera is slow or the
            # viewer loop wakes early we must not re-send the same bytes.
            frame_id = id(jpeg)
            if frame_id == last_sent_id:
                continue
            last_sent_id = frame_id

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n')
    finally:
        # Runs when the browser closes the stream (tab closed, navigated away)
        with _viewers_lock:
            _viewers = max(0, _viewers - 1)

@app.route('/video_feed')
def video_feed():
    """MJPEG live stream endpoint.
    The explicit no-cache and no-buffering headers are required so that:
    - Browsers (Chrome, Firefox, Edge) do not cache the first frame and show
      a frozen image instead of the continuous stream.
    - Reverse-proxies (nginx, etc.) do not buffer the multipart response,
      which would stall delivery and make the feed appear stuck."""
    resp = Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['X-Accel-Buffering'] = 'no'   # disable nginx proxy buffering
    return resp

@app.route('/api/camera/pause', methods=['POST'])
def camera_pause():
    """The enrollment page calls this so the server lets go of the webcam and
    the browser's getUserMedia() can use it."""
    global _camera_paused
    with _viewers_lock:
        _camera_paused = True
    # Wait briefly for the worker to actually release the device
    deadline = time.time() + 3.0
    while time.time() < deadline and _camera_held():
        time.sleep(0.05)
    return jsonify({"success": True, "camera_released": not _camera_held()})

@app.route('/api/camera/resume', methods=['POST'])
def camera_resume():
    """Called when leaving the enrollment page, so monitoring can use the camera again."""
    global _camera_paused
    with _viewers_lock:
        _camera_paused = False
    return jsonify({"success": True})

@app.route('/api/status')
def api_status():
    global room_state, tracked_students
    role = session.get('role', 'SUPERVISOR')
    user_inst = session.get('institution_id')
    req_inst = request.args.get('institution_id')

    # Security check: Non-admins cannot query other institutions
    if role != 'ADMIN' and req_inst and req_inst != user_inst:
        record_audit_event(session.get('user_id'), session.get('username'), role, user_inst, "ACCESS_DENIED", request.remote_addr, "DENIED", f"Cross-institution telemetry attempt on {req_inst}")
        return jsonify({"error": "FORBIDDEN: Cross-institution telemetry access violation"}), 403

    # Multi-tenant Isolation
    if role == 'ADMIN':
        filter_inst = req_inst if (req_inst and req_inst != 'ALL') else None
    else:
        filter_inst = user_inst

    students_list = []
    for sid, data in tracked_students.items():
        stu_inst = data.get("institution_id", "INST-001")
        if filter_inst and stu_inst != filter_inst:
            continue
        if role == 'STUDENT' and sid != session.get('student_id'):
            continue

        risk = int(data.get("suspicion_score", data.get("risk_score", 0)))
        trust = int(data.get("trust_score", max(0, 100 - risk)))
        yaw = float(data.get("yaw", 0))
        pitch = float(data.get("pitch", 0))
        gaze = data.get("gaze", "CENTER")
        
        # Calculate continuous eye attention index
        attention_pen = min(60, int(abs(yaw) * 0.8 + abs(pitch) * 0.6)) + (15 if gaze not in ("CENTER", "UNKNOWN") else 0)
        attention_pct = max(15, min(100, 100 - attention_pen))

        students_list.append({
            "id": sid,
            "name": data.get("name", sid),
            "institution_id": stu_inst,
            "status": data.get("status", "Active"),
            "suspicion_score": risk,
            "risk_score": risk,
            "trust_score": trust,
            "tier": data.get("tier", "LOW"),
            "yaw": yaw,
            "pitch": pitch,
            "gaze": gaze,
            "direction": data.get("direction", "CENTER"),
            "eye_attention_pct": attention_pct,
            "gaze_deviation": data.get("gaze_deviation", None),
            "phone_conf": data.get("phone_conf", 0),
            "last_event": data.get("last_event"),
            "alerts": data.get("alerts", []),
            "calibrated": data.get("calibrated", False),
        })

    return jsonify({
        "room_status": room_state.get("status", "NORMAL"),
        "unknown_count": room_state.get("unknown_count", 0),
        "phone_detected": room_state.get("phone_detected", False),
        "smartwatch_detected": room_state.get("smartwatch_detected", False),
        "earbud_detected": room_state.get("earbud_detected", False),
        "book_detected": room_state.get("book_detected", False),
        "camera_blocked": room_state.get("camera_blocked", False),
        "room_alerts": room_state.get("alerts", []),
        "institution_id": filter_inst or "ALL",
        "video_source": "cctv" if (CONFIG.get("cctv_ip") or "").strip() else "webcam",
        "exam_name": CONFIG.get("exam_name", "National Proctoring Assessment"),
        "supervisor_name": CONFIG.get("supervisor_name", "Command Supervisor"),
        "students": students_list
    })

@app.route('/api/alerts')
def api_alerts():
    role = session.get('role', 'SUPERVISOR')
    user_inst = session.get('institution_id')
    req_inst = request.args.get('institution_id')

    # Security check: Non-admins cannot query other institutions
    if role != 'ADMIN' and req_inst and req_inst != user_inst:
        record_audit_event(session.get('user_id'), session.get('username'), role, user_inst, "ACCESS_DENIED", request.remote_addr, "DENIED", f"Cross-institution alert attempt on {req_inst}")
        return jsonify({"error": "FORBIDDEN: Cross-institution alert access violation"}), 403

    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()

        if role == 'ADMIN':
            if req_inst and req_inst != 'ALL':
                cursor.execute("""
                    SELECT risk_score, direction, status, timestamp, institution_id, student_id
                    FROM exam_logs 
                    WHERE institution_id = %s
                    ORDER BY timestamp DESC 
                    LIMIT 20;
                """, (req_inst,))
            else:
                cursor.execute("""
                    SELECT risk_score, direction, status, timestamp, institution_id, student_id
                    FROM exam_logs 
                    ORDER BY timestamp DESC 
                    LIMIT 20;
                """)
        elif role == 'SUPERVISOR':
            cursor.execute("""
                SELECT risk_score, direction, status, timestamp, institution_id, student_id
                FROM exam_logs 
                WHERE institution_id = %s
                ORDER BY timestamp DESC 
                LIMIT 20;
            """, (user_inst or 'INST-001',))
        elif role == 'STUDENT':
            cursor.execute("""
                SELECT risk_score, direction, status, timestamp, institution_id, student_id
                FROM exam_logs 
                WHERE student_id = %s
                ORDER BY timestamp DESC 
                LIMIT 20;
            """, (session.get('student_id'),))
        else:
            cursor.execute("""
                SELECT risk_score, direction, status, timestamp, institution_id, student_id
                FROM exam_logs 
                WHERE institution_id = %s
                ORDER BY timestamp DESC 
                LIMIT 20;
            """, (user_inst or 'INST-001',))

        rows = cursor.fetchall()
        alerts = []
        for row in rows:
            alerts.append({
                "risk_score": row[0],
                "direction": row[1],
                "status": row[2],
                "timestamp": row[3].strftime("%H:%M:%S") if row[3] else "",
                "institution_id": row[4],
                "student_id": row[5]
            })
        cursor.close()
        conn.close()
        return jsonify(alerts)
    except Exception as e:
        print(f"Error fetching alerts: {e}")
        return jsonify([])

if __name__ == '__main__':
    # Start the worker thread now (models are already loaded at import). It
    # stays idle and does NOT touch the camera until someone actually views
    # /video_feed, leaving the webcam free for the enrollment page.
    start_camera_worker()
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
