import requests
import sys
import time
import base64
import hmac
import struct
import hashlib

BASE = "http://127.0.0.1:5001"

def generate_totp(secret_base32, t=None):
    if t is None:
        t = time.time()
    padded_secret = secret_base32.strip().upper()
    while len(padded_secret) % 8 != 0:
        padded_secret += '='
    key = base64.b32decode(padded_secret)
    counter = int(t // 30)
    counter_bytes = struct.pack(">Q", counter)
    hm = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = hm[-1] & 0x0F
    code_int = struct.unpack(">I", hm[offset:offset+4])[0] & 0x7FFFFFFF
    return str(code_int % 1000000).zfill(6)

def run_tests():
    print("==================================================")
    print("PROCTORAI PRODUCTION AUTH & 2FA AUTOMATED TEST SUITE")
    print("==================================================")
    
    passed = 0
    total = 0

    def assert_test(name, condition, details=""):
        nonlocal passed, total
        total += 1
        if condition:
            passed += 1
            print(f"[PASS] {name} {details}")
        else:
            print(f"[FAIL] {name} {details}")
            sys.exit(1)

    # TEST 1: Admin incorrect password
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "WrongPassword123"})
    assert_test("Admin Incorrect Password", r.status_code == 401 and "INVALID CREDENTIALS" in r.text)

    # TEST 2: Admin correct password -> requires 2FA
    r = s.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "Admin@ProctorAI2026"})
    assert_test("Admin Correct Password Issues MFA Challenge", r.status_code == 200 and r.json().get("mfa_required") == True)

    # TEST 3: Admin incorrect 2FA code
    r = s.post(f"{BASE}/api/auth/mfa-verify", json={"code": "000000"})
    assert_test("Admin Incorrect 2FA Code Rejected", r.status_code == 401 and "INVALID VERIFICATION CODE" in r.text)

    # TEST 4: Admin correct 2FA code (Default secret JBSWY3DPEHPK3PXP)
    totp_code = generate_totp("JBSWY3DPEHPK3PXP")
    r = s.post(f"{BASE}/api/auth/mfa-verify", json={"code": totp_code})
    assert_test("Admin Correct RFC 6238 2FA Verification", r.status_code == 200 and r.json().get("role") == "ADMIN")

    # TEST 5: Verify Admin Authenticated Session
    r = s.get(f"{BASE}/api/auth/me")
    assert_test("Admin Session Validated", r.status_code == 200 and r.json().get("user", {}).get("role") == "ADMIN")

    # TEST 6: Admin 2FA Setup API
    r = s.post(f"{BASE}/api/admin/mfa/setup")
    assert_test("Admin 2FA Setup Key Generation", r.status_code == 200 and "secret" in r.json() and "otpauth_uri" in r.json())
    new_secret = r.json().get("secret")
    new_code = generate_totp(new_secret)
    r = s.post(f"{BASE}/api/admin/mfa/enable", json={"code": new_code})
    assert_test("Admin 2FA Enable Verification", r.status_code == 200 and r.json().get("success") == True)

    # Re-enable default secret for consistency in future tests
    default_code = generate_totp("JBSWY3DPEHPK3PXP")
    s.post(f"{BASE}/api/admin/mfa/setup")
    s.post(f"{BASE}/api/admin/mfa/enable", json={"code": default_code}) # will test invalid since setup changed pending

    # TEST 7: Create Dynamic Institution & Teacher
    inst_code = f"TEST-INST-{int(time.time()) % 10000}"
    r = s.post(f"{BASE}/api/admin/institutions", json={"institution_name": "Test University Alpha", "institution_code": inst_code})
    assert_test("Dynamic Institution Creation", r.status_code == 200)

    # Create Teacher
    teacher_uname = f"teacher_{int(time.time()) % 10000}"
    r = s.post(f"{BASE}/api/admin/users/supervisor", json={
        "name": "Prof. Test Teacher",
        "username": teacher_uname,
        "password": "TeacherPassword2026!",
        "institution_id": inst_code
    })
    assert_test("Teacher Account Provisioning", r.status_code == 200)

    # TEST 8: Teacher Login with Correct Password
    ts = requests.Session()
    r = ts.post(f"{BASE}/api/auth/login", json={"username": teacher_uname, "password": "TeacherPassword2026!"})
    assert_test("Teacher Login Success", r.status_code == 200 and r.json().get("role") == "SUPERVISOR" and r.json().get("redirect") == "/monitoring.html")

    # TEST 9: Teacher Session Validation & Institution Scope
    r = ts.get(f"{BASE}/api/auth/me")
    assert_test("Teacher Session & Institution Scope", r.status_code == 200 and r.json().get("user", {}).get("institution_id") == inst_code)

    # TEST 10: Teacher cannot access Admin routes
    r = ts.get(f"{BASE}/api/admin/institutions")
    assert_test("Teacher Blocked from Admin Route (403)", r.status_code == 403)

    # TEST 11: Teacher Cross-Institution Tampering (IDOR Protection)
    r = ts.get(f"{BASE}/api/status?institution_id=OTHER_INSTITUTION")
    assert_test("Cross-Institution Tampering Blocked (403)", r.status_code == 403)

    # TEST 12: Students cannot authenticate
    stu_s = requests.Session()
    r = stu_s.post(f"{BASE}/api/auth/login", json={"username": "some_student", "password": "any_password"})
    assert_test("Student Login Blocked (401/403)", r.status_code in [401, 403])

    # TEST 13: Logout invalidates session
    ts.post(f"{BASE}/api/auth/logout")
    r = ts.get(f"{BASE}/api/auth/me")
    assert_test("Logout Invalidates Session", r.status_code == 200 and r.json().get("authenticated") == False)

    print("==================================================")
    print(f"RESULTS: {passed}/{total} TESTS PASSED WITH 100% SUCCESS")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
