# ProctorAI Demo Login System

## Overview

The ProctorAI project now includes a **temporary demo login gate** for hackathon demonstration purposes. This is a frontend-only authentication system that allows quick access to the platform without requiring backend infrastructure.

---

## 🔐 Demo Authentication Details

### **IMPORTANT: This is NOT production-ready authentication!**

This demo login system is designed exclusively for:
- **Hackathon demonstrations**
- **Local development testing**
- **UI/UX prototyping**

**DO NOT use this in production environments!**

---

## How It Works

### 1. Login Screen (`login.html`)

The entry point to the ProctorAI platform featuring:
- **Professional enterprise UI design** (monochromatic with steel blue accents)
- **Email/Gmail validation** (must be valid email format)
- **Password requirement** (minimum 3 characters for UX testing)
- **NO hardcoded credentials** - accepts ANY valid email + ANY password

### 2. Validation Rules

#### Email Validation:
- ✅ Required field
- ✅ Must match basic email regex: `name@domain.com`
- ✅ Examples that work: `test@gmail.com`, `demo@university.edu`, `admin@company.org`

#### Password Validation:
- ✅ Required field
- ✅ Minimum 3 characters (for demo convenience)
- ❌ NO password storage
- ❌ NO password comparison
- ❌ NO server validation

### 3. Session Management

After successful login, the system stores:
```javascript
sessionStorage.setItem('proctorai_demo_auth', 'true');
sessionStorage.setItem('proctorai_demo_email', email);
localStorage.setItem('proctorai_demo_auth', 'true');
localStorage.setItem('proctorai_demo_email', email);
```

**What is stored:**
- ✅ Authentication flag (`true`/`false`)
- ✅ User email address (for display purposes only)

**What is NOT stored:**
- ❌ Passwords
- ❌ API keys
- ❌ Tokens
- ❌ Credentials
- ❌ Secrets

---

## Protected Pages

The following pages are protected and require demo authentication:

| Page | Protected | Authentication Check |
|------|-----------|---------------------|
| `login.html` | ❌ No | Public entry point |
| `index.html` | ❌ No | Public landing page |
| `monitoring.html` | ✅ Yes | Checks `proctorai_demo_auth` |
| `enrollment.html` | ✅ Yes | Checks `proctorai_demo_auth` |
| `admin.html` | ✅ Yes | Checks `proctorai_demo_auth` |

### Authentication Check Implementation

Protected pages include this check at the start of their `<script>` section:

```javascript
// Demo Authentication Check
(function checkAuth() {
    const demoAuth = sessionStorage.getItem('proctorai_demo_auth') || 
                     localStorage.getItem('proctorai_demo_auth');
    if (demoAuth !== 'true') {
        window.location.href = 'login.html';
    }
})();
```

---

## Logout Flow

Users can log out from the monitoring dashboard:

1. Click the **Logout** button
2. System clears session data:
   ```javascript
   sessionStorage.removeItem('proctorai_demo_auth');
   sessionStorage.removeItem('proctorai_demo_email');
   localStorage.removeItem('proctorai_demo_auth');
   localStorage.removeItem('proctorai_demo_email');
   ```
3. Redirects to `login.html?logged_out=true`
4. Displays success message: "Signed out successfully"

---

## Testing Checklist

### ✅ Login Screen Tests

- [x] **Valid email + any password** → enters project
- [x] **Empty email** → shows error "Please enter an email address"
- [x] **Invalid email format** → shows error "Please enter a valid email address"
- [x] **Empty password** → shows error "Password is required"
- [x] **Short password** → shows error "Password is too short"
- [x] **Login transition** → smooth animation to monitoring dashboard
- [x] **Responsive design** → works on desktop/tablet/mobile

### ✅ Authentication Flow Tests

- [x] **Direct access to protected pages** → redirects to login
- [x] **Access after login** → allows entry to all protected pages
- [x] **Logout** → clears session and returns to login
- [x] **Session persistence** → remains logged in after page refresh
- [x] **Multiple tabs** → authentication state syncs across tabs

### ✅ UI/UX Tests

- [x] **Professional design** → premium enterprise aesthetic
- [x] **Password visibility toggle** → eye icon shows/hides password
- [x] **Error messages** → clear, professional, non-technical
- [x] **Loading state** → button shows spinner during transition
- [x] **Success feedback** → confirmation before redirect
- [x] **No console errors** → clean browser console
- [x] **No horizontal overflow** → proper responsive behavior

---

## Security Disclaimer

### ⚠️ IMPORTANT LIMITATIONS

This demo login system is **NOT secure** and should **NEVER** be used in production:

| Security Feature | Demo Status | Production Requirement |
|-----------------|-------------|----------------------|
| Password hashing | ❌ None | ✅ bcrypt/Argon2 required |
| Database authentication | ❌ None | ✅ PostgreSQL/MongoDB required |
| API authentication | ❌ None | ✅ JWT/OAuth2 required |
| 2FA/MFA | ❌ None | ✅ TOTP/SMS required |
| Session tokens | ❌ None | ✅ Signed tokens required |
| CSRF protection | ❌ None | ✅ CSRF tokens required |
| Rate limiting | ❌ None | ✅ Required (prevent brute force) |
| SQL injection protection | ❌ N/A | ✅ Parameterized queries required |
| XSS protection | ⚠️ Basic | ✅ Content Security Policy required |

---

## Future Production Implementation

When ready for production, replace the demo login with:

### 1. **Backend Authentication**
```python
# Example: Flask + bcrypt
from flask_bcrypt import Bcrypt
from flask_login import LoginManager

bcrypt = Bcrypt(app)
login_manager = LoginManager(app)

@app.route('/api/auth/login', methods=['POST'])
def login():
    email = request.json.get('email')
    password = request.json.get('password')
    
    user = User.query.filter_by(email=email).first()
    if user and bcrypt.check_password_hash(user.password_hash, password):
        login_user(user)
        return jsonify({'success': True})
    return jsonify({'success': False}), 401
```

### 2. **Database Schema**
```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL,
    institution_id INTEGER REFERENCES institutions(id),
    mfa_secret VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP
);
```

### 3. **JWT Token System**
```javascript
// Client-side: Store JWT in httpOnly cookie
// Server-side: Verify JWT on each request
const token = jwt.sign(
    { userId: user.id, role: user.role },
    process.env.JWT_SECRET,
    { expiresIn: '1h' }
);
```

### 4. **Multi-Factor Authentication**
```python
# Add TOTP for admin/teacher accounts
import pyotp

def verify_2fa(user, token):
    totp = pyotp.TOTP(user.mfa_secret)
    return totp.verify(token)
```

---

## GitHub Security

### ✅ Verified Safe to Commit

The following files are **safe** and included in version control:
- ✅ `login.html` (no secrets)
- ✅ `monitoring.html` (authentication check only)
- ✅ `enrollment.html` (authentication check only)
- ✅ `admin.html` (authentication check only)
- ✅ `.env.example` (template only, no real values)

### ❌ NEVER Commit These Files

The `.gitignore` properly excludes:
- ❌ `.env` (real credentials)
- ❌ `*.key`, `*.pem`, `*.cert` (certificates)
- ❌ `credentials.json` (API keys)
- ❌ `config.json` (application secrets)
- ❌ `*.db` (local databases with user data)
- ❌ `bench_probe.jpg` (captured face images)

---

## Usage Instructions

### For Developers

1. **Clone the repository**
   ```bash
   git clone https://github.com/Codenama-007/Face-Detection-Project-using-opencv.git
   cd Face-Detection-Project-using-opencv
   ```

2. **Open `login.html` in a browser**
   ```bash
   # Option 1: Direct file access
   open login.html
   
   # Option 2: Local server (recommended)
   python -m http.server 8000
   # Then navigate to http://localhost:8000/login.html
   ```

3. **Login with any credentials**
   - Email: `test@gmail.com` (or any valid email)
   - Password: `demo123` (or any password ≥3 chars)

4. **Explore the platform**
   - View monitoring dashboard
   - Test enrollment system
   - Check admin panel (demo mode)

### For Judges/Reviewers

1. Navigate to the deployed GitHub Pages URL (if available)
2. Enter any email address (e.g., `judge@hackathon.com`)
3. Enter any password (e.g., `demo`)
4. Click "Enter ProctorAI"
5. Explore the full platform capabilities

---

## Design Philosophy

### Why This Approach?

1. **Rapid Demonstration** - No backend setup required
2. **Platform Showcase** - Focus on AI features, not auth infrastructure
3. **Easy Testing** - Judges can access immediately
4. **Clear Separation** - Demo auth is isolated and replaceable
5. **Professional UX** - Enterprise-grade login experience

### Design Principles

- **Minimal Friction** - No registration, no verification delays
- **Professional Appearance** - Looks and feels like production
- **Clear Labeling** - "Demo Access" badge indicates temporary nature
- **Easy Transition** - Code structure allows clean replacement
- **Security Conscious** - No false security claims

---

## Troubleshooting

### Issue: "Redirects to login after entering credentials"

**Cause:** Browser blocking `sessionStorage`/`localStorage`

**Solution:**
```javascript
// Check browser console for errors
// Try in incognito/private mode
// Ensure cookies/storage not blocked
```

### Issue: "Can't access protected pages"

**Cause:** Authentication state not persisting

**Solution:**
1. Open browser DevTools (F12)
2. Go to Application/Storage tab
3. Check `sessionStorage` and `localStorage`
4. Verify `proctorai_demo_auth` = `"true"`

### Issue: "Login screen shows in different language"

**Solution:** Login screen uses English by default (as specified in requirements)

---

## License & Attribution

ProctorAI © 2026 - Hackathon Demonstration Project

This demo authentication system is provided as-is for educational and demonstration purposes only.

---

## Contact & Support

For questions about the demo authentication system:
- **Repository:** [Codenama-007/Face-Detection-Project-using-opencv](https://github.com/Codenama-007/Face-Detection-Project-using-opencv)
- **Issues:** Use GitHub Issues for bug reports
- **Security:** Do NOT use in production without proper authentication implementation

---

**Last Updated:** August 16, 2026  
**Version:** 1.0 (Hackathon Demo)  
**Status:** ✅ Demo Ready | ❌ Not Production Ready
