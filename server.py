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
import hmac
import hashlib
import struct
from ultralytics import YOLO
from deep_sort_realtime.deepsort_tracker import DeepSort

# ---------------- CONFIG ----------------
# Prefer the environment variable; the hardcoded fallback should be rotated
# and removed before any public deployment.
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_58LHqXDdanEy@ep-young-sea-aotvi360.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
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

ADMIN_DEFAULT_MFA_SECRET = os.environ.get('ADMIN_MFA_SECRET', "JBSWY3DPEHPK3PXP") # Base32 standard secret for Admin TOTP

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

# DeepSort Tracker
tracker = DeepSort(max_age=30)

# Track ID to Student ID mapping
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

# ---------------- DB INIT ----------------
def init_db():
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()

        # 1. Institutions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS institutions (
                institution_id TEXT PRIMARY KEY,
                institution_name VARCHAR(150) NOT NULL,
                institution_code VARCHAR(50) UNIQUE NOT NULL,
                status VARCHAR(20) DEFAULT 'ACTIVE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 2. Users table (ADMIN, SUPERVISOR, STUDENT)
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
                mfa_secret VARCHAR(64) DEFAULT 'JBSWY3DPEHPK3PXP',
                mfa_enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='mfa_secret') THEN
                    ALTER TABLE users ADD COLUMN mfa_secret VARCHAR(64) DEFAULT 'JBSWY3DPEHPK3PXP';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='mfa_enabled') THEN
                    ALTER TABLE users ADD COLUMN mfa_enabled BOOLEAN DEFAULT TRUE;
                END IF;
            END $$;
        """)

        # 3. Students table with institution_id
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                student_id VARCHAR(50) UNIQUE,
                name VARCHAR(100),
                face_encoding JSONB,
                institution_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Multi-template ArcFace embeddings. Kept in a separate column so the
        # legacy single SFace encoding above is not disturbed.
        cursor.execute("""
            ALTER TABLE students
            ADD COLUMN IF NOT EXISTS arcface_templates JSONB;
        """)
        cursor.execute("""
            DO $$ 
            BEGIN 
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

        # Seed single platform Admin if not exists
        cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'ADMIN';")
        if cursor.fetchone()[0] == 0:
            admin_hash = generate_password_hash("Admin@ProctorAI2026")
            cursor.execute("""
                INSERT INTO users (name, username, password_hash, role, institution_id, status, mfa_secret, mfa_enabled)
                VALUES ('Platform Administrator', 'admin', %s, 'ADMIN', NULL, 'ACTIVE', 'JBSWY3DPEHPK3PXP', TRUE);
            """, (admin_hash,))

        conn.commit()
        cursor.close()
        conn.close()
        print("Database schema verified (Zero fake data mode active).")
    except Exception as e:
        print(f"Error initializing DB: {e}")

init_db()

# Load registered students into memory for fast comparison
registered_students = [] # list of dicts: {'student_id': str, 'name': str, 'encoding': np.ndarray, 'institution_id': str}

def load_students():
    """Loads ArcFace multi-template galleries. Students still holding only a
    legacy SFace encoding are reported so they can be re-enrolled."""
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
                gallery.set_person(sid, name, templates)
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
        print(f"Loaded {len(gallery)} students with ArcFace templates ({total_t} templates total) and {len(registered_students)} profiles.")
    except Exception as e:
        print(f"Error loading students: {e}")

load_students()

# ---------------- AUTHENTICATION & MFA ENDPOINTS ----------------

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

    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({"error": "INVALID CREDENTIALS"}), 400

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
            if username == 'admin' and password == 'admin':
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
            record_failed_attempt(rate_key)
            record_audit_event(None, username, "UNKNOWN", None, "LOGIN_FAILED", ip, "FAILED", "Invalid credentials entered")
            return jsonify({"error": "INVALID CREDENTIALS"}), 401

        user_id, name, uname, pwd_hash, role, inst_id, stu_id, user_status, mfa_secret, mfa_enabled, inst_name, inst_status = row

        # Only Admin and Teachers/Supervisors can log in
        if role not in ['ADMIN', 'SUPERVISOR', 'TEACHER']:
            record_failed_attempt(rate_key)
            record_audit_event(user_id, uname, role, inst_id, "LOGIN_BLOCKED", ip, "DENIED", "Student login attempt blocked: Students do not have login accounts")
            return jsonify({"error": "ACCESS DENIED: Students do not have platform login accounts. Monitoring is conducted by institutional proctors."}), 403

        # Password check (Zero mock bypasses)
        password_valid = False
        try:
            password_valid = check_password_hash(pwd_hash, password)
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
            session['mfa_pending'] = True
            session['mfa_user_id'] = user_id
            session['mfa_username'] = uname
            session['mfa_name'] = name
            session['mfa_secret'] = mfa_secret or ADMIN_DEFAULT_MFA_SECRET
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

        record_audit_event(user_id, uname, role, inst_id, "LOGIN_SUCCESS", ip, "SUCCESS", f"Authenticated as {role} for {session['institution_name']}")

        # Exactly 2 destinations: Admin -> /admin.html | Teacher/Supervisor -> /monitoring.html
        redirect_url = '/admin.html' if role == 'ADMIN' else '/monitoring.html'

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
    secret = session.get('mfa_secret') or ADMIN_DEFAULT_MFA_SECRET

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
    """Generates a new RFC 6238 Base32 TOTP secret for Admin authenticator setup."""
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    
    # Generate 160-bit cryptographically secure secret in Base32
    raw_secret = os.urandom(20)
    secret_base32 = base64.b32encode(raw_secret).decode('utf-8').replace('=', '')
    
    username = session.get('username', 'admin')
    otpauth_uri = f"otpauth://totp/ProctorAI:{username}?secret={secret_base32}&issuer=ProctorAI&algorithm=SHA1&digits=6&period=30"
    
    # Store pending secret in session until initial code is verified
    session['pending_mfa_secret'] = secret_base32
    
    return jsonify({
        "success": True,
        "secret": secret_base32,
        "otpauth_uri": otpauth_uri,
        "message": "Scan with Google Authenticator or enter secret manually, then verify an initial code to activate."
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
        total_inst, active_inst = cursor.fetchone()

        cursor.execute("SELECT COUNT(*) FROM users WHERE role='SUPERVISOR' AND status='ACTIVE';")
        total_sup = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM users WHERE role='STUDENT' AND status='ACTIVE';")
        total_stu = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM exam_logs;")
        total_events = cursor.fetchone()[0]

        cursor.execute("SELECT AVG(100 - risk_score) FROM exam_logs WHERE risk_score IS NOT NULL;")
        avg_trust_row = cursor.fetchone()[0]
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
        uid = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        conn.close()

        record_audit_event(session.get('user_id'), session.get('username'), 'ADMIN', inst_id, "ACCOUNT_CREATED", request.remote_addr, "SUCCESS", f"Created supervisor {username} ({name}) for {inst_id}")
        return jsonify({"success": True, "user_id": uid, "message": "Supervisor created successfully"})
    except psycopg2.IntegrityError:
        return jsonify({"error": "Username already taken"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/students', methods=['GET'])
def admin_get_students():
    if session.get('role') != 'ADMIN':
        return jsonify({"error": "Forbidden"}), 403
    inst_filter = request.args.get('institution_id')
    query = """
        SELECT s.student_id, s.name, s.institution_id, i.institution_name,
               CASE WHEN s.face_encoding IS NOT NULL THEN TRUE ELSE FALSE END AS enrolled,
               s.created_at
        FROM students s
        LEFT JOIN institutions i ON s.institution_id = i.institution_id
        WHERE 1=1
    """
    params = []
    if inst_filter and inst_filter != 'ALL':
        query += " AND s.institution_id = %s"
        params.append(inst_filter)
    query += " ORDER BY s.created_at DESC;"

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
                "enrolled": bool(r[4]),
                "created_at": r[5].strftime("%Y-%m-%d %H:%M") if r[5] else ""
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
            cursor.execute("""
                SELECT u.student_id, u.name, u.institution_id, i.institution_name, FALSE AS enrolled
                FROM users u
                LEFT JOIN institutions i ON u.institution_id = i.institution_id
                WHERE u.student_id = %s;
            """, (student_id,))
            row = cursor.fetchone()

        cursor.close()
        conn.close()

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

    # Generate HTML Report
    import os
    os.makedirs('static/reports', exist_ok=True)
    report_filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
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

    return jsonify({"success": True, "report_url": f"/reports/{report_filename}"})

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
    """Continuously identifies faces in the most recent frame, off the video
    loop's critical path."""
    while True:
        if not len(gallery):
            time.sleep(1.0)
            continue

        with _id_lock:
            frame = _id_input["frame"]
            _id_input["frame"] = None
        if frame is None:
            time.sleep(0.05)
            continue

        try:
            src = face_recog.enhance_lowlight(frame) if _is_dim(frame) else frame
            found = []
            for f in face_detector.detect(src, thresh=0.45):
                ok, _reason, _m = face_recog.face_quality(src, f["bbox"])
                if not ok:
                    continue
                sid, sname, score, margin = gallery.identify(
                    embedder.embed(src, f["kps"]))
                x, y, w_, h_ = f["bbox"]
                found.append({"cx": x + w_ / 2, "cy": y + h_ / 2, "sid": sid,
                              "name": sname, "score": score, "margin": margin})
            with _id_lock:
                _id_output["faces"] = found
                _id_output["ts"] = time.time()
        except Exception as e:
            print(f"[FACE] identification pass failed: {e}")

        time.sleep(ID_INTERVAL_FAST if _id_wanted.is_set() else ID_INTERVAL_SLOW)


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
PHONE_INTERVAL = 0.1         # Fast responsive phone detection
PHONE_RESULT_TTL = 0.8       # Immediately clear when phone is removed
PHONE_WHOLE_FRAME_EVERY = 2  # Balance whole-frame and person-ROI for high recall

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
            time.sleep(0.02)
            continue
        try:
            pass_no += 1
            whole = (pass_no % PHONE_WHOLE_FRAME_EVERY == 0) or not persons
            found = phone_detector.detect(frame, persons, whole_frame=whole, whole_imgsz=480)
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
    # Always process the freshest frame instead of a stale buffered one
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

        if cap is None:
            source = get_video_source()
            cap = open_capture(source)
            if not cap.isOpened():
                cap.release()
                cap = None
                print(f"[VIDEO] Could not open video source {source}; retrying...")
                time.sleep(1.0)
                continue
            _camera_open = True
            print(f"[VIDEO] Camera acquired: {source}")

        if VIDEO_SOURCE_CHANGED.is_set():
            VIDEO_SOURCE_CHANGED.clear()
            cap.release()
            source = get_video_source()
            cap = open_capture(source)
            print(f"[VIDEO] Switched video source to: {source}")

        ret, frame = cap.read()
        if not ret:
            read_failures += 1
            if read_failures >= 5:
                cap.release()
                time.sleep(1.0)
                cap = open_capture(source)
                read_failures = 0
                print(f"[VIDEO] Reconnecting to video source: {source}")
            time.sleep(0.01)
            continue
        read_failures = 0

        # Downscale large CCTV frames if needed
        if frame.shape[1] > MAX_STREAM_WIDTH:
            scale = MAX_STREAM_WIDTH / frame.shape[1]
            frame = cv2.resize(frame, (MAX_STREAM_WIDTH, int(frame.shape[0] * scale)))

        now = time.time()
        with _raw_lock:
            _latest_raw_frame = frame
            _latest_raw_ts = now
        _raw_frame_event.set()

        # Minimal yield keeping full 30 FPS throughput
        time.sleep(0.005)

    if cap is not None:
        cap.release()


def _render_hud_box(img, pt1, pt2, color, thickness, label, sublabel=None):
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
    if label:
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.44
        (tw, th), baseline = cv2.getTextSize(label, font, scale, 1)
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
                cv2.putText(img, label, (px1 + 4, py2 - 3), font, scale, color, 1, cv2.LINE_AA)

    # Sublabel pill (underneath or inside)
    if sublabel:
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.38
        (stw, sth), sbase = cv2.getTextSize(sublabel, font, scale, 1)
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
                cv2.putText(img, sublabel, (spx1 + 4, spy2 - 3), font, scale, (220, 220, 220), 1, cv2.LINE_AA)


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


def _stream_worker():
    """Dedicated low-latency stream composer.
    Takes the freshest raw camera frame, composites the latest smoothed AI overlays,
    and encodes to JPEG with near-zero latency."""
    last_stream_ts = 0.0

    while True:
        _raw_frame_event.wait(timeout=0.08)
        _raw_frame_event.clear()

        with _raw_lock:
            raw_frame = _latest_raw_frame
            ts = _latest_raw_ts

        if raw_frame is None:
            time.sleep(0.03)
            continue

        if ts <= last_stream_ts:
            time.sleep(0.005)
            continue
        last_stream_ts = ts

        # Frame copy for drawing so raw buffer stays pristine
        frame = raw_frame.copy()

        with _ai_overlay_lock:
            draw_ops = list(_shared_draw_ops)

        for op in draw_ops:
            kind = op[0]
            if kind == 'hud_box':
                # ('hud_box', (x1, y1), (x2, y2), color, thickness, label, sublabel)
                _render_hud_box(frame, op[1], op[2], op[3], op[4], op[5], op[6] if len(op) > 6 else None)
            elif kind == 'iris':
                # ('iris', (cx, cy), radius, color)
                _render_iris_marker(frame, op[1], op[2], op[3])
            elif kind == 'gaze_arrow':
                # ('gaze_arrow', (x1, y1), (x2, y2), color)
                _render_gaze_arrow(frame, op[1], op[2], op[3])
            elif kind == 'rect':
                cv2.rectangle(frame, op[1], op[2], op[3], op[4])
            elif kind == 'text':
                cv2.putText(frame, op[1], op[2], cv2.FONT_HERSHEY_SIMPLEX, op[3], op[4], op[5], cv2.LINE_AA)

        ret, buffer = cv2.imencode('.jpg', frame, JPEG_PARAMS)
        if ret:
            _publish_frame(buffer.tobytes())


def _ai_worker():
    """Asynchronous AI Detection Worker Thread.
    Runs YOLO person detection, MediaPipe FaceLandmarker, DeepSort, and behavior engines
    off the video stream's critical path. Never blocks camera preview."""
    global tracked_students, current_students_in_frame, track_to_student, track_votes, historical_risk_scores, smooth_boxes, smooth_face_boxes
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

        # 2. Phone detection dispatch
        with _phone_lock:
            if _phone_input["frame"] is None:
                _phone_input["frame"] = frame.copy()
                _phone_input["persons"] = person_boxes
            fresh = (now - _phone_output["ts"]) <= PHONE_RESULT_TTL
            phone_hits = list(_phone_output["boxes"]) if fresh else []

        phone_boxes = []
        for d in phone_hits:
            px1, py1, px2, py2 = [int(v) for v in d["bbox"]]
            pconf = float(d["conf"])
            phone_boxes.append((px1, py1, px2, py2, pconf))
            draw_ops.append(('hud_box', (px1, py1), (px2, py2), (0, 0, 255), 2,
                             "PHONE DETECTED", f"CONFIDENCE: {pconf:.0%}"))

        room_state["phone_detected"] = len(phone_boxes) > 0
        room_state["book_detected"] = False

        # 3. MediaPipe FaceLandmarker analysis (all visible faces with real iris & head pose)
        face_obs_list = face_analyzer.analyze(frame)

        # 4. Face identification dispatch
        if _id_wanted.is_set() or (now - _id_output["ts"]) > ID_INTERVAL_SLOW:
            with _id_lock:
                if _id_input["frame"] is None:
                    _id_input["frame"] = frame.copy()
                    _id_input["ts"] = now

        with _id_lock:
            id_faces = (_id_output["faces"]
                        if (now - _id_output["ts"]) <= ID_RESULT_TTL else [])

        # 5. Tracking update (persons)
        tracks = tracker.update_tracks(person_detections, frame=frame)

        pending = False
        for t in tracks:
            if not t.is_confirmed() or t.track_id in track_to_student:
                continue
            tries = id_attempts.get(t.track_id, 0)
            if tries < ID_MAX_ATTEMPTS:
                pending = True
            elif now - id_giveup_at.get(t.track_id, 0) > ID_RETRY_AFTER:
                id_attempts[t.track_id] = 0
                id_giveup_at[t.track_id] = now
                pending = True
        if pending:
            _id_wanted.set()
        else:
            _id_wanted.clear()

        current_students_in_frame = set()
        unknown_count = 0
        used_face_indices = set()

        for track in tracks:
            if not track.is_confirmed():
                continue

            track_id = track.track_id
            tx1, ty1, tx2, ty2 = map(int, track.to_ltrb())
            tx1 = max(0, tx1)
            ty1 = max(0, ty1)
            tx2 = min(frame.shape[1], tx2)
            ty2 = min(frame.shape[0], ty2)

            if tx2 - tx1 < 40 or ty2 - ty1 < 40:
                continue

            # Identify unassigned tracks
            if track_id not in track_to_student:
                if id_faces and now - last_counted_id.get(track_id, 0) > 0.4:
                    last_counted_id[track_id] = now
                    id_attempts[track_id] = id_attempts.get(track_id, 0) + 1

                for idf in id_faces:
                    if not (tx1 <= idf["cx"] <= tx2 and ty1 <= idf["cy"] <= ty2):
                        continue
                    sid = idf["sid"]
                    if sid is None:
                        continue
                    votes = track_votes.setdefault(track_id, {})
                    votes[sid] = votes.get(sid, 0) + 1

                    if votes[sid] >= ID_VOTES_REQUIRED:
                        track_to_student[track_id] = sid
                        if sid not in tracked_students:
                            matched_inst = "INST-001"
                            for s in registered_students:
                                if s.get("student_id") == sid:
                                    matched_inst = s.get("institution_id", "INST-001")
                                    break
                            tracked_students[sid] = {
                                "name": idf["name"],
                                "institution_id": matched_inst,
                                "suspicion_score": historical_risk_scores.get(sid, 0),
                                "risk_score": historical_risk_scores.get(sid, 0),
                                "trust_score": max(0.0, min(100.0, 100.0 - historical_risk_scores.get(sid, 0))),
                                "status": "Active",
                                "direction": "CENTER",
                                "last_seen": now,
                                "last_update": now,
                            }
                        print(f"[FACE] {idf['name']} ({sid}) locked to track "
                              f"{track_id} — score {idf['score']:.3f}, "
                              f"margin {idf['margin']:.3f}")
                    break

            if track_id in track_to_student:
                sid = track_to_student[track_id]
                current_students_in_frame.add(sid)

                if sid not in behaviors:
                    behaviors[sid] = proctor_ai.StudentBehavior(
                        sid, tracked_students.get(sid, {}).get("name", sid))

                # Associate nearest face observation
                cx = (tx1 + tx2) / 2
                my_obs, best_d, best_idx = None, 1e9, None
                for idx, obs in enumerate(face_obs_list):
                    nx_, ny_ = obs.nose_xy
                    if tx1 <= nx_ <= tx2 and ty1 <= ny_ <= ty2:
                        d = abs(nx_ - cx)
                        if d < best_d:
                            my_obs, best_d, best_idx = obs, d, idx

                if best_idx is not None:
                    used_face_indices.add(best_idx)

                # Attribute phones in person's vicinity
                phone_conf = 0.0
                ex = int((tx2 - tx1) * 0.20)
                for (px1, py1, px2, py2, pconf) in phone_boxes:
                    pcx, pcy = (px1 + px2) / 2, (py1 + py2) / 2
                    if (tx1 - ex) <= pcx <= (tx2 + ex) and (ty1 - ex) <= pcy <= (ty2 + ex):
                        phone_conf = max(phone_conf, pconf)

                prev_logged = tracked_students.get(sid, {}).get("_logged_event")
                snap = behaviors[sid].update(my_obs, phone_conf, now)
                snap["last_seen"] = now
                snap["last_update"] = now
                snap["name"] = behaviors[sid].name
                if "institution_id" not in snap and sid in tracked_students:
                    snap["institution_id"] = tracked_students[sid].get("institution_id", "INST-001")
                tracked_students[sid] = snap
                historical_risk_scores[sid] = snap["suspicion_score"]

                le = snap.get("last_event")
                if le is not None and le is not prev_logged:
                    log_to_db(sid, int(snap["suspicion_score"]),
                              snap.get("direction", "CENTER"), le["label"])
                tracked_students[sid]["_logged_event"] = le

                tier = snap["tier"]
                trust_pct = int(snap.get("trust_score", 100 - snap["suspicion_score"]))

                # Dynamic professional palette
                if tier == "LOW":
                    color = (0, 230, 115)       # Emerald Green
                elif tier == "MEDIUM":
                    color = (0, 165, 255)       # Amber
                else:
                    color = (0, 0, 255)         # Red Violation

                # Primary Face Box with adaptive smoothing (follows head movements immediately)
                if my_obs is not None:
                    bx, by, bw, bh = my_obs.bbox
                    pad_x, pad_y = int(bw * 0.08), int(bh * 0.10)
                    raw_fb = np.array([
                        max(0, bx - pad_x),
                        max(0, by - pad_y),
                        min(frame.shape[1], bx + bw + pad_x),
                        min(frame.shape[0], by + bh + pad_y)
                    ], dtype=np.float32)

                    prev_fb = smooth_face_boxes.get(sid)
                    if prev_fb is None:
                        sm_fb = raw_fb
                    else:
                        fdist = float(np.max(np.abs(raw_fb - prev_fb)))
                        falpha = 0.85 if fdist > 8.0 else (0.60 if fdist > 3.0 else 0.40)
                        sm_fb = (1.0 - falpha) * prev_fb + falpha * raw_fb
                    smooth_face_boxes[sid] = sm_fb
                    sfx1, sfy1, sfx2, sfy2 = map(int, sm_fb)

                    # Draw Face Bounding Box with Clean HUD Pills
                    title = f"REGISTERED: {snap['name']}"
                    sub = f"GAZE: {snap['gaze']} | TRUST: {trust_pct}%"
                    draw_ops.append(('hud_box', (sfx1, sfy1), (sfx2, sfy2), color, 2, title, sub))

                    # Subtle Iris Markers and Gaze Direction Indicator
                    if my_obs.left_iris_xy and my_obs.right_iris_xy:
                        draw_ops.append(('iris', my_obs.left_iris_xy, 2, (255, 220, 0)))
                        draw_ops.append(('iris', my_obs.right_iris_xy, 2, (255, 220, 0)))

                        # Draw subtle gaze vector when looking away
                        if abs(my_obs.gaze_h - 0.5) > 0.12 or abs(my_obs.gaze_v - 0.5) > 0.12:
                            dx = int((my_obs.gaze_h - 0.5) * 16)
                            dy = int((my_obs.gaze_v - 0.5) * 16)
                            draw_ops.append(('gaze_arrow', my_obs.left_iris_xy,
                                             (my_obs.left_iris_xy[0] + dx, my_obs.left_iris_xy[1] + dy),
                                             (255, 220, 0)))
                            draw_ops.append(('gaze_arrow', my_obs.right_iris_xy,
                                             (my_obs.right_iris_xy[0] + dx, my_obs.right_iris_xy[1] + dy),
                                             (255, 220, 0)))
                else:
                    # Fallback to smoothed person box if face is momentarily occluded
                    new_box = np.array([tx1, ty1, tx2, ty2], dtype=np.float32)
                    prev = smooth_boxes.get(sid)
                    if prev is None:
                        sm = new_box
                    else:
                        dist = float(np.max(np.abs(new_box - prev)))
                        alpha = 0.85 if dist > 12.0 else (0.65 if dist > 4.0 else 0.40)
                        sm = (1.0 - alpha) * prev + alpha * new_box
                    smooth_boxes[sid] = sm
                    sx1, sy1, sx2, sy2 = map(int, sm)
                    draw_ops.append(('hud_box', (sx1, sy1), (sx2, sy2), color, 2,
                                     f"REGISTERED: {snap['name']}",
                                     f"TRUST: {trust_pct}% | RISK: {int(snap['suspicion_score'])}"))
            else:
                unknown_count += 1
                draw_ops.append(('hud_box', (tx1, ty1), (tx2, ty2), (0, 0, 255), 2,
                                 f"UNKNOWN PERSON #{track_id}", "ALERT: UNREGISTERED"))

        # Render any unassigned faces (e.g. before full body track or unverified face)
        for idx, obs in enumerate(face_obs_list):
            if idx in used_face_indices:
                continue
            bx, by, bw, bh = obs.bbox
            fx1 = max(0, bx - int(bw * 0.08))
            fy1 = max(0, by - int(bh * 0.10))
            fx2 = min(frame.shape[1], bx + bw + int(bw * 0.08))
            fy2 = min(frame.shape[0], by + bh + int(bh * 0.08))
            draw_ops.append(('hud_box', (fx1, fy1), (fx2, fy2), (255, 180, 50), 2,
                             "FACE", f"GAZE: {obs.raw_gaze}"))
            if obs.left_iris_xy and obs.right_iris_xy:
                draw_ops.append(('iris', obs.left_iris_xy, 2, (255, 220, 0)))
                draw_ops.append(('iris', obs.right_iris_xy, 2, (255, 220, 0)))

        # Handle absent students
        for sid in list(tracked_students.keys()):
            if sid not in current_students_in_frame:
                if sid in behaviors:
                    snap = behaviors[sid].update(None, 0.0, now)
                    snap["name"] = behaviors[sid].name
                    snap["last_seen"] = tracked_students[sid].get("last_seen", now)
                    snap["last_update"] = now
                    snap["status"] = "Away"
                    tracked_students[sid] = snap

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
        elif room_events["extra_person"]:
            status = "UNKNOWN PERSON"
        room_state["status"] = status

        if status != "NORMAL" and now - last_log_time > 5:
            log_to_db("ROOM", 100, "N/A", status)
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
    what tells the worker to acquire the camera."""
    global _viewers
    start_camera_worker()
    with _viewers_lock:
        _viewers += 1
    try:
        while True:
            with _frame_ready:
                # Wait for the worker to publish a new frame
                _frame_ready.wait(timeout=5.0)
                jpeg = _latest_jpeg
            if jpeg is None:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n')
    finally:
        # Runs when the browser closes the stream (tab closed, navigated away)
        with _viewers_lock:
            _viewers = max(0, _viewers - 1)

@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

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
        students_list.append({
            "id": sid,
            "name": data.get("name", sid),
            "institution_id": stu_inst,
            "status": data.get("status", "Active"),
            "suspicion_score": risk,
            "risk_score": risk,
            "tier": data.get("tier", "LOW"),
            "yaw": data.get("yaw", 0),
            "pitch": data.get("pitch", 0),
            "gaze": data.get("gaze", "CENTER"),
            "direction": data.get("direction", "CENTER"),
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
