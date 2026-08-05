import cv2
import time
import json
import os
import hmac
import hashlib
import secrets
import threading
import numpy as np
import psycopg2
from flask import Flask, Response, jsonify, send_from_directory, request, session, redirect, url_for
from flask_cors import CORS
from datetime import datetime
import base64
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
app.secret_key = CONFIG["secret_key"]
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

# ---------------- MIDDLEWARE ----------------
# Endpoints reachable without a session (login, first-run setup, Face ID sign-in).
# NOTE: /api/webauthn/register/* is deliberately NOT here - enrolling a new
# Face ID authenticator requires an already-authenticated session, otherwise
# anyone could bind their own face to the account.
PUBLIC_API = {
    "/api/supervisor_login",
    "/api/setup",
    "/api/setup/status",
    "/api/webauthn/status",
    "/api/webauthn/login/begin",
    "/api/webauthn/login/complete",
}

# Login/setup/Face ID gating is disabled for now so the dashboard is
# reachable directly while face-detection accuracy work is the focus.
# All the auth code below (setup wizard, password login, WebAuthn) is left
# intact - flip this back to True to re-enable the login wall.
REQUIRE_LOGIN = False

@app.before_request
def require_auth():
    if not REQUIRE_LOGIN:
        return

    # Only protect API endpoints, video feed, monitoring, and enrollment.
    # We do NOT protect the index, login/setup pages, static assets, or the
    # authentication endpoints themselves.
    protected_html = ['/monitoring.html', '/enrollment.html']

    if request.endpoint in ['supervisor_login', 'serve_index']:
        return

    path = request.path
    if path in protected_html or path.startswith('/video_feed') or (path.startswith('/api/') and path not in PUBLIC_API):
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
import proctor_ai
import face_recog

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

# DeepSort Tracker
tracker = DeepSort(max_age=30)

# Track ID to Student ID mapping
track_to_student = {}
track_votes = {} # track_id -> {student_id: count}
historical_risk_scores = {}
head_pose_buffers = {}
baseline_calibration = {} # sid -> {"nx": [], "ny": []}
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
        # Multi-template ArcFace embeddings. Kept in a separate column so the
        # legacy single SFace encoding above is not disturbed.
        cursor.execute("""
            ALTER TABLE students
            ADD COLUMN IF NOT EXISTS arcface_templates JSONB;
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
    """Loads ArcFace multi-template galleries. Students still holding only a
    legacy SFace encoding are reported so they can be re-enrolled."""
    global registered_students
    registered_students = []
    gallery.people.clear()
    legacy_only = []
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT student_id, name, face_encoding, arcface_templates FROM students;")
        rows = cursor.fetchall()
        for sid, name, legacy_enc, arc in rows:
            if arc:
                templates = np.array(arc, dtype=np.float32)
                if templates.ndim == 1:
                    templates = templates[None, :]
                gallery.set_person(sid, name, templates)
                registered_students.append({"student_id": sid, "name": name,
                                            "templates": len(templates)})
            elif legacy_enc is not None:
                legacy_only.append(f"{name} ({sid})")
        cursor.close()
        conn.close()
        total_t = sum(len(p["templates"]) for p in gallery.people.values())
        print(f"Loaded {len(gallery)} students with ArcFace templates "
              f"({total_t} templates total).")
        if legacy_only:
            print(f"  {len(legacy_only)} student(s) still on the old SFace "
                  f"encoding and will NOT be recognised until re-enrolled: "
                  f"{', '.join(legacy_only[:5])}"
                  + (" ..." if len(legacy_only) > 5 else ""))
    except Exception as e:
        print(f"Error loading students: {e}")

load_students()

# ---------------- ENDPOINTS ----------------

def _apply_cctv_choice(data):
    """Applies the CCTV/webcam choice sent with a login request.

    If the request contains a 'cctv_ip' key: a non-empty value switches the
    feed to that CCTV stream, an empty value switches back to the webcam.
    """
    if "cctv_ip" not in data:
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

@app.route('/api/supervisor_login', methods=['POST'])
def supervisor_login():
    if not CONFIG.get("setup_complete"):
        return jsonify({"error": "First-run setup required.", "setup_required": True}), 403

    data = request.json or {}
    username = (data.get('username') or "").strip()
    password = data.get('password') or ""

    # Bypass logic: Allow any username and password for now
    session['admin_logged_in'] = True
    _apply_cctv_choice(data)
    return jsonify({"success": True, "message": "Logged in successfully (Bypass enabled)"})

# ---------------- WINDOWS HELLO / FACE ID (WebAuthn) ----------------
# WebAuthn is the web standard for platform authenticators. The browser shows
# the Windows Hello (Face ID / fingerprint / PIN) prompt on the CLIENT machine
# and proves it cryptographically to us. This is the correct model for a web
# app: a previous server-side implementation using winrt prompted on the
# SERVER machine, which both fails outside UWP apps and would let a remote
# visitor trigger a prompt on the host.

def _rp_id():
    """The WebAuthn Relying Party ID must be the origin's hostname (no port)."""
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
    """Tells the login page whether to offer Face ID sign-in for this origin."""
    rp = _rp_id()
    registered = [c for c in _get_credentials() if c.get("rp_id") == rp]
    return jsonify({"registered": len(registered) > 0, "rp_id": rp})

@app.route('/api/webauthn/register/begin', methods=['POST'])
def webauthn_register_begin():
    """Starts Face ID enrollment. Requires an authenticated session, so only
    someone who already proved the password can bind a new authenticator."""
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
            # platform = the built-in Windows Hello authenticator
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    session['webauthn_challenge'] = _b64url_encode(opts.challenge)
    return Response(options_to_json(opts), mimetype='application/json')

@app.route('/api/webauthn/register/complete', methods=['POST'])
def webauthn_register_complete():
    from webauthn import verify_registration_response

    expected = session.pop('webauthn_challenge', None)
    if not expected:
        return jsonify({"error": "Registration session expired. Please try again."}), 400

    try:
        verification = verify_registration_response(
            credential=request.get_data(as_text=True),
            expected_challenge=_b64url_decode(expected),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
        )
    except Exception as e:
        return jsonify({"error": f"Face ID registration failed: {e}"}), 400

    creds = _get_credentials()
    # Replace any existing credential for this same authenticator/origin
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

@app.route('/api/webauthn/login/begin', methods=['POST'])
def webauthn_login_begin():
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor, UserVerificationRequirement,
    )

    if not CONFIG.get("setup_complete"):
        return jsonify({"error": "First-run setup required.", "setup_required": True}), 403

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

@app.route('/api/webauthn/login/complete', methods=['POST'])
def webauthn_login_complete():
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

    try:
        verification = verify_authentication_response(
            credential=json.dumps(body),
            expected_challenge=_b64url_decode(expected),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
            credential_public_key=_b64url_decode(stored["public_key"]),
            credential_current_sign_count=stored.get("sign_count", 0),
            require_user_verification=True,
        )
    except Exception as e:
        return jsonify({"error": f"Face ID verification failed: {e}"}), 401

    # Persist the new signature counter (clone-detection)
    stored["sign_count"] = verification.new_sign_count
    save_config(CONFIG)

    session['admin_logged_in'] = True
    _apply_cctv_choice(body.get("extra") or {})
    return jsonify({"success": True, "message": "Signed in with Windows Hello"})

@app.route('/api/supervisor_logout', methods=['POST', 'GET'])
def supervisor_logout():
    session.pop('admin_logged_in', None)
    return jsonify({"success": True})

def _decode_b64_image(image_b64):
    if ',' in image_b64:
        image_b64 = image_b64.split(',')[1]
    nparr = np.frombuffer(base64.b64decode(image_b64), np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)


@app.route('/api/register', methods=['POST'])
def register():
    """Multi-template enrolment.

    Accepts `images` (a list of frames covering different angles) or a single
    `image` for backward compatibility. One photo cannot represent a face
    across pose and lighting, so more frames directly raise accuracy.
    """
    data = request.json or {}
    student_id = data.get('student_id')
    name = data.get('name')
    images_b64 = data.get('images') or ([data['image']] if data.get('image') else [])

    if not student_id or not name or not images_b64:
        return jsonify({"error": "Missing fields"}), 400

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
            INSERT INTO students (student_id, name, arcface_templates)
            VALUES (%s, %s, %s)
            ON CONFLICT (student_id) DO UPDATE
              SET name=EXCLUDED.name, arcface_templates=EXCLUDED.arcface_templates;
        """, (student_id, name, json.dumps([t.tolist() for t in kept])))
        conn.commit()
        cursor.close()
        conn.close()

        load_students()
        msg = (f"Enrolled {name} with {len(kept)} face templates from "
               f"{len(images_b64)} frames.")
        if rejected:
            msg += f" Skipped {len(rejected)} unusable frame(s)."
        return jsonify({"success": True, "message": msg,
                        "templates": len(kept), "rejected": rejected})
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
    global SESSION_ACTIVE
    SESSION_ACTIVE = False
    
    # Generate HTML Report
    import os
    os.makedirs('static/reports', exist_ok=True)
    report_filename = f"report_{datetime.now().strftime('%Y%md_%H%M%S')}.html"
    report_path = os.path.join('static/reports', report_filename)
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Examination Integrity Report</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
        <style>
            body {{ font-family: 'Inter', sans-serif; background: #050505; color: #fff; padding: 3rem; line-height: 1.6; }}
            h1 {{ border-bottom: 1px solid #333; padding-bottom: 1rem; color: #0a84ff; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 2rem; }}
            th, td {{ padding: 1rem; text-align: left; border-bottom: 1px solid #222; }}
            th {{ background: #111; color: #888; text-transform: uppercase; font-size: 0.85rem; }}
            .high-risk {{ color: #ff453a; font-weight: bold; }}
            .med-risk {{ color: #ffd60a; font-weight: bold; }}
            .low-risk {{ color: #32d74b; font-weight: bold; }}
        </style>
    </head>
    <body>
        <h1>Examination Integrity Report</h1>
        <p><strong>Generated:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <table>
            <thead>
                <tr>
                    <th>Student ID</th>
                    <th>Name</th>
                    <th>Final Risk Score</th>
                    <th>Last Status</th>
                </tr>
            </thead>
            <tbody>
    """
    
    for sid, data in tracked_students.items():
        score = int(data['risk_score'])
        if score > 75: risk_class = "high-risk"
        elif score > 25: risk_class = "med-risk"
        else: risk_class = "low-risk"
        
        html_content += f"""
                <tr>
                    <td>{sid}</td>
                    <td>{data['name']}</td>
                    <td class="{risk_class}">{score}%</td>
                    <td>{data['status']}</td>
                </tr>
        """
        
    html_content += """
            </tbody>
        </table>
    </body>
    </html>
    """
    
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

# Performance tuning
# Run the AI on every Nth frame; in-between frames replay the last overlays.
# Measured per-AI-frame cost on this CPU: YOLO 36ms + DeepSort 28ms +
# FaceLandmarker 5ms. Safe to skip frames because every behaviour rule is
# expressed in seconds of wall-clock time, not in frame counts.
PROCESS_EVERY = 3
# 480 keeps small objects (phones) detectable. 320 is ~2x faster but starts
# missing them; raise PROCESS_EVERY before lowering this.
YOLO_IMGSZ = 480
MAX_STREAM_WIDTH = 960   # downscale larger (CCTV) frames before processing
JPEG_PARAMS = [int(cv2.IMWRITE_JPEG_QUALITY), 70]

# Face identification runs on its OWN thread. Measured cost is ~180ms for
# SCRFD plus ~170ms for ArcFace, which would otherwise halve the video frame
# rate. Identity does not change frame to frame, so the video loop consumes
# whatever the identifier last produced and never waits for it.
ID_INTERVAL_FAST = 0.5   # seconds between passes while someone is unidentified
ID_INTERVAL_SLOW = 3.0   # seconds between passes once everyone is known
ID_VOTES_REQUIRED = 3    # consistent matches before an identity is locked
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
    _id_thread_started = True
    threading.Thread(target=_identification_worker, name="face-id",
                     daemon=True).start()
    print("[FACE] identification worker started")

def open_capture(source):
    # Measured on this machine: the default backend opens the webcam in
    # ~0.16s vs ~0.95s for CAP_DSHOW, and both then run at ~30 FPS.
    cap = cv2.VideoCapture(source)
    # Always process the freshest frame instead of a stale buffered one
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap

# ---- Shared frame buffer -------------------------------------------------
# Exactly ONE worker thread owns the camera and runs the AI pipeline; every
# HTTP viewer reads the latest encoded JPEG from here. Previously the camera
# was opened inside the per-request generator, so each browser tab/refresh/
# reconnect created another capture handle AND another full AI pipeline on
# the same camera.
_latest_jpeg = None
_frame_ready = threading.Condition()
_worker_lock = threading.Lock()
_worker_started = False

# A local webcam can only be held by one process at a time. The enrollment
# page captures the student's face with getUserMedia() in the BROWSER, so the
# server must let go of the camera whenever nobody is watching /video_feed
# (or while the enrollment page explicitly asks for it).
_viewers = 0
_viewers_lock = threading.Lock()
_camera_paused = False
_camera_open = False         # True while the worker actually holds the device
IDLE_RELEASE_SECONDS = 2.0   # release the camera this long after the last viewer leaves

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

def _camera_worker():
    global tracked_students, current_students_in_frame, track_to_student, track_votes, historical_risk_scores, head_pose_buffers, baseline_calibration, SESSION_ACTIVE, _camera_open
    cap = None
    source = None
    last_log_time = 0
    frame_idx = 0
    read_failures = 0
    idle_since = None

    # Overlays computed on AI frames, replayed on skipped frames
    draw_ops = []
    phone_detected = False
    book_detected = False

    while True:
        # ---- Acquire / release the camera based on demand ----------------
        if not _camera_wanted():
            if cap is not None:
                # Pausing (enrollment needs the webcam) releases immediately;
                # merely having no viewers waits out the idle grace period so
                # a page refresh doesn't thrash the device open/closed.
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
                    reason = "enrollment paused it" if _camera_paused else "no viewers"
                    print(f"[VIDEO] Camera released ({reason}) - free for the browser/other apps.")
            time.sleep(0.1)
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

        # Hot-swap the capture when the supervisor switches webcam <-> CCTV
        if VIDEO_SOURCE_CHANGED.is_set():
            VIDEO_SOURCE_CHANGED.clear()
            cap.release()
            source = get_video_source()
            cap = open_capture(source)
            print(f"[VIDEO] Switched video source to: {source}")

        ret, frame = cap.read()
        if not ret:
            # CCTV/RTSP streams drop frames or disconnect; reconnect
            # instead of killing the feed
            read_failures += 1
            if read_failures >= 5:
                cap.release()
                time.sleep(1.0)
                cap = open_capture(source)
                read_failures = 0
                print(f"[VIDEO] Reconnecting to video source: {source}")
            continue
        read_failures = 0

        # Downscale large CCTV frames: all downstream AI gets faster and
        # the MJPEG stream gets lighter
        if frame.shape[1] > MAX_STREAM_WIDTH:
            scale = MAX_STREAM_WIDTH / frame.shape[1]
            frame = cv2.resize(frame, (MAX_STREAM_WIDTH, int(frame.shape[0] * scale)))

        now = time.time()
        frame_idx += 1

        if frame_idx % PROCESS_EVERY != 0:
            # Skipped frame: replay the last overlays and stream immediately
            for op in draw_ops:
                if op[0] == 'rect':
                    cv2.rectangle(frame, op[1], op[2], op[3], op[4])
                else:
                    cv2.putText(frame, op[1], op[2], cv2.FONT_HERSHEY_SIMPLEX, op[3], op[4], op[5])
            ret, buffer = cv2.imencode('.jpg', frame, JPEG_PARAMS)
            _publish_frame(buffer.tobytes())
            continue

        draw_ops = []

        # 1. YOLO11 object detection: persons for tracking, phones for alerts.
        #    Only actual phones are considered (COCO class 67) - books,
        #    bottles, calculators etc. are deliberately ignored to keep the
        #    false-positive rate down.
        yolo_results = yolo_model(frame, stream=True, verbose=False, imgsz=YOLO_IMGSZ)
        person_detections = []
        phone_boxes = []   # (x1, y1, x2, y2, conf)

        for r in yolo_results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])

                if cls_id == 0 and conf > 0.5: # person
                    person_detections.append(([x1, y1, x2 - x1, y2 - y1], conf, "person"))
                elif cls_id == 67 and conf >= proctor_ai.CONF_PHONE: # cell phone
                    phone_boxes.append((x1, y1, x2, y2, conf))
                    draw_ops.append(('rect', (x1, y1), (x2, y2), (0, 0, 255), 3))
                    draw_ops.append(('text', f"PHONE {conf:.0%}", (x1, y1 - 10), 0.8, (0, 0, 255), 3))

        room_state["phone_detected"] = len(phone_boxes) > 0
        room_state["book_detected"] = False

        # 2. Landmark face analysis - ONE FaceLandmarker pass for the whole
        #    frame gives every face's head pose (yaw/pitch/roll), iris gaze
        #    and mouth activity.
        face_obs_list = face_analyzer.analyze(frame)

        # 2b. Hand the current frame to the identification thread and pick up
        #     whatever it last produced. This never blocks the video loop.
        if _id_wanted.is_set() or (now - _id_output["ts"]) > ID_INTERVAL_SLOW:
            with _id_lock:
                if _id_input["frame"] is None:
                    _id_input["frame"] = frame.copy()
                    _id_input["ts"] = now

        with _id_lock:
            id_faces = (_id_output["faces"]
                        if (now - _id_output["ts"]) <= ID_RESULT_TTL else [])

        # 3. DeepSort Tracking (stable per-student IDs)
        tracks = tracker.update_tracks(person_detections, frame=frame)

        # Tell the identifier whether to run at the fast or slow cadence
        if any(t.is_confirmed() and t.track_id not in track_to_student
               for t in tracks):
            _id_wanted.set()
        else:
            _id_wanted.clear()

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
            
            # Identify: match any face recognised this pass whose centre falls
            # inside this track's box. Votes still gate the lock so a single
            # frame can never assign an identity.
            if track_id not in track_to_student:
                for idf in id_faces:
                    if not (tx1 <= idf["cx"] <= tx2 and ty1 <= idf["cy"] <= ty2):
                        continue
                    sid = idf["sid"]
                    if sid is None:          # below threshold or ambiguous
                        continue
                    votes = track_votes.setdefault(track_id, {})
                    votes[sid] = votes.get(sid, 0) + 1

                    if votes[sid] >= ID_VOTES_REQUIRED:
                        track_to_student[track_id] = sid
                        if sid not in tracked_students:
                            tracked_students[sid] = {
                                "name": idf["name"],
                                "suspicion_score": historical_risk_scores.get(sid, 0),
                                "risk_score": historical_risk_scores.get(sid, 0),
                                "status": "Active",
                                "direction": "CENTER",
                                "last_seen": now,
                                "last_update": now,
                            }
                        print(f"[FACE] {idf['name']} ({sid}) locked to track "
                              f"{track_id} — score {idf['score']:.3f}, "
                              f"margin {idf['margin']:.3f}")
                    break

            # If identified, run the full behaviour pipeline
            if track_id in track_to_student:
                sid = track_to_student[track_id]
                current_students_in_frame.add(sid)

                if sid not in behaviors:
                    behaviors[sid] = proctor_ai.StudentBehavior(
                        sid, tracked_students.get(sid, {}).get("name", sid))

                # -- associate a face observation with this track (nose point
                #    inside the track box; nearest to centre wins) --
                cx = (tx1 + tx2) / 2
                my_obs, best_d = None, 1e9
                for obs in face_obs_list:
                    nx_, ny_ = obs.nose_xy
                    if tx1 <= nx_ <= tx2 and ty1 <= ny_ <= ty2:
                        d = abs(nx_ - cx)
                        if d < best_d:
                            my_obs, best_d = obs, d

                # -- attribute phones to this student (phone centre inside a
                #    slightly expanded person box) --
                phone_conf = 0.0
                ex = int((tx2 - tx1) * 0.20)
                for (px1, py1, px2, py2, pconf) in phone_boxes:
                    pcx, pcy = (px1 + px2) / 2, (py1 + py2) / 2
                    if (tx1 - ex) <= pcx <= (tx2 + ex) and (ty1 - ex) <= pcy <= (ty2 + ex):
                        phone_conf = max(phone_conf, pconf)

                # -- temporal behaviour + suspicion update --
                prev_logged = tracked_students.get(sid, {}).get("_logged_event")
                snap = behaviors[sid].update(my_obs, phone_conf, now)
                snap["last_seen"] = now
                snap["last_update"] = now
                snap["name"] = behaviors[sid].name
                tracked_students[sid] = snap
                historical_risk_scores[sid] = snap["suspicion_score"]

                # DB-log each alert exactly once (the engine reuses the same
                # last_event object until a NEW alert is confirmed)
                le = snap.get("last_event")
                if le is not None and le is not prev_logged:
                    log_to_db(sid, int(snap["suspicion_score"]),
                              snap.get("direction", "CENTER"), le["label"])
                tracked_students[sid]["_logged_event"] = le

                # -- EMA-smoothed box so it doesn't jump between frames --
                new_box = np.array([tx1, ty1, tx2, ty2], dtype=np.float32)
                prev = smooth_boxes.get(sid)
                sm = new_box if prev is None else 0.6 * prev + 0.4 * new_box
                smooth_boxes[sid] = sm
                sx1, sy1, sx2, sy2 = map(int, sm)

                tier = snap["tier"]
                color = (0, 255, 0)
                if tier == "MEDIUM":
                    color = (0, 165, 255)
                elif tier in ("HIGH", "CRITICAL"):
                    color = (0, 0, 255)

                label = f"{snap['name']} [{tier}] {int(snap['suspicion_score'])}"
                sub = f"yaw {snap['yaw']:+.0f}  pitch {snap['pitch']:+.0f}  gaze {snap['gaze']}"
                draw_ops.append(('rect', (sx1, sy1), (sx2, sy2), color, 2))
                draw_ops.append(('text', label, (sx1, sy1 - 28), 0.55, color, 2))
                draw_ops.append(('text', sub, (sx1, sy1 - 8), 0.45, (200, 200, 200), 1))
            else:
                unknown_count += 1
                draw_ops.append(('rect', (tx1, ty1), (tx2, ty2), (0, 0, 255), 2))
                draw_ops.append(('text', f"UNKNOWN {track_id}", (tx1, ty1 - 10), 0.6, (0, 0, 255), 2))

        # Students not visible this frame: keep their temporal engine running
        # (drives FACE_MISSING) and drop them after 60s away
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
                    to_delete = [tid for tid, s in track_to_student.items() if s == sid]
                    for tid in to_delete:
                        del track_to_student[tid]
                        if tid in track_votes:
                            del track_votes[tid]

        # ---- room-level temporal events (also never single-frame) ----
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

        # Render overlays and encode the frame
        for op in draw_ops:
            if op[0] == 'rect':
                cv2.rectangle(frame, op[1], op[2], op[3], op[4])
            else:
                cv2.putText(frame, op[1], op[2], cv2.FONT_HERSHEY_SIMPLEX, op[3], op[4], op[5])

        ret, buffer = cv2.imencode('.jpg', frame, JPEG_PARAMS)
        _publish_frame(buffer.tobytes())

    cap.release()

def start_camera_worker():
    """Starts the single camera/AI thread. Called at server startup so the
    camera is already open and warm before anyone logs in."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_camera_worker, name="camera-worker", daemon=True).start()
    print("[VIDEO] Camera worker thread started (camera warming up).")
    start_identification_worker()

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

    students_list = []
    for sid, data in tracked_students.items():
        students_list.append({
            "id": sid,
            "name": data.get("name", sid),
            "status": data.get("status", ""),
            "suspicion_score": data.get("suspicion_score", data.get("risk_score", 0)),
            "risk_score": data.get("risk_score", 0),          # legacy key
            "tier": data.get("tier", "LOW"),
            "yaw": data.get("yaw", 0),
            "pitch": data.get("pitch", 0),
            "gaze": data.get("gaze", "CENTER"),
            "direction": data.get("direction", "CENTER"),
            "phone_conf": data.get("phone_conf", 0),
            "last_event": data.get("last_event"),
            "alerts": data.get("alerts", []),
            "calibrated": data.get("calibrated", False),
        })

    return jsonify({
        "room_status": room_state["status"],
        "unknown_count": room_state["unknown_count"],
        "phone_detected": room_state.get("phone_detected", False),
        "camera_blocked": room_state.get("camera_blocked", False),
        "room_alerts": room_state.get("alerts", []),
        "students": students_list,
        "video_source": "cctv" if (CONFIG.get("cctv_ip") or "").strip() else "webcam",
        "exam_name": CONFIG.get("exam_name", ""),
        "supervisor_name": CONFIG.get("supervisor_name", "")
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
    # Start the worker thread now (models are already loaded at import). It
    # stays idle and does NOT touch the camera until someone actually views
    # /video_feed, leaving the webcam free for the enrollment page.
    start_camera_worker()
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
