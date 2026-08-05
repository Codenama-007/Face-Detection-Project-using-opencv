// DOM Elements
const studentsGrid = document.getElementById('studentsGrid');
const globalStatusBanner = document.getElementById('globalStatusBanner');
const globalStatusText = document.getElementById('globalStatusText');
const unknownCountBadge = document.getElementById('unknownCountBadge');
const alertsList = document.getElementById('alertsList');

let displayedStudents = new Set();
let previousAlerts = [];

// Initialize
function init() {
    addAlert('Dashboard initialized. Waiting for feed...', 'info');
    
    // Check Session Status immediately and then every second
    checkSessionStatus();
    setInterval(fetchLiveStatus, 1000);
    setInterval(checkSessionStatus, 2000);

    const endBtn = document.getElementById('endSessionBtn');
    
    if(endBtn) {
        endBtn.addEventListener('click', async () => {
            if(!confirm('End this examination session and generate report?')) return;
            const res = await fetch('/api/session/end', { method: 'POST' });
            if(res.ok) {
                const data = await res.json();
                checkSessionStatus();
                addAlert('Session ended. Report generated!', 'info');
                // Open report in new tab
                window.open(data.report_url, '_blank');
            }
        });
    }
}

async function checkSessionStatus() {
    try {
        const res = await fetch('/api/session/status');
        const data = await res.json();
        
        const dot = document.getElementById('sessionStatusDot');
        const text = document.getElementById('globalStatusText');
        
        if(data.active) {
            dot.style.background = '#32d74b'; // green
            dot.style.boxShadow = '0 0 10px #32d74b';
            text.innerText = 'SESSION ACTIVE';
        } else {
            dot.style.background = '#64748b'; // gray
            dot.style.boxShadow = '0 0 10px #64748b';
            text.innerText = 'SESSION PAUSED/ENDED';
        }
    } catch(err) {
        console.error("Failed to check session status", err);
    }
}
// Tier -> theme status color + label (label always shown: never color alone)
const TIER_STYLE = {
    LOW:      { cls: 'success', label: 'LOW' },
    MEDIUM:   { cls: 'warning', label: 'MEDIUM' },
    HIGH:     { cls: 'danger',  label: 'HIGH' },
    CRITICAL: { cls: 'danger',  label: 'CRITICAL' },
};

// Update the grid of students
function updateStudentsGrid(students) {
    const currentFrameStudents = new Set(students.map(s => s.id));

    // Remove students no longer tracked
    for (let sid of displayedStudents) {
        if (!currentFrameStudents.has(sid)) {
            const card = document.getElementById(`student-card-${sid}`);
            if (card) card.remove();
            displayedStudents.delete(sid);
            addAlert(`Student ${sid} left the frame.`, 'warning');
        }
    }

    if (students.length === 0) {
        if (studentsGrid.innerHTML.trim() === '') {
            studentsGrid.innerHTML = '<div style="text-align:center; color:#94a3b8; font-size:0.9rem; padding:2rem 0;">Waiting for students to enter the frame...</div>';
        }
        return;
    }

    if (studentsGrid.innerHTML.includes('Waiting for students')) {
        studentsGrid.innerHTML = '';
    }

    students.forEach(student => {
        let card = document.getElementById(`student-card-${student.id}`);
        const tier = TIER_STYLE[student.tier] || TIER_STYLE.LOW;
        const score = Math.round(student.suspicion_score ?? student.risk_score ?? 0);
        const scorePct = Math.min(100, score); // meter caps at 100 visually
        const phone = (student.phone_conf || 0) > 0 ? `${student.phone_conf}%` : '—';
        const calib = student.calibrated === false ? ' (calibrating)' : '';
        const le = student.last_event;
        const lastEventText = le
            ? `${le.label} · ${le.time} · ${le.confidence}%`
            : 'No suspicious events';

        if (!card) {
            addAlert(`Identified: ${student.name} (${student.id})`, 'success');
            displayedStudents.add(student.id);
            card = document.createElement('div');
            card.id = `student-card-${student.id}`;
            card.className = 'card glass-card stat-card';
            card.style.cssText = 'display:flex;flex-direction:column;gap:0.6rem;padding:1rem;';
            card.innerHTML = `
                <div style="display:flex;align-items:center;justify-content:space-between;gap:0.75rem;">
                    <div style="display:flex;align-items:center;gap:0.75rem;min-width:0;">
                        <div class="sc-avatar" style="width:38px;height:38px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;background:var(--${tier.cls});">${student.name.charAt(0)}</div>
                        <div style="min-width:0;">
                            <h4 style="margin:0;font-size:0.95rem;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${student.name}</h4>
                            <p style="margin:0;font-size:0.75rem;color:var(--text-secondary);">${student.id}</p>
                        </div>
                    </div>
                    <span class="sc-tier badge" style="background:rgba(255,255,255,0.06);color:var(--${tier.cls});border:1px solid var(--${tier.cls});font-size:0.7rem;">${tier.label}</span>
                </div>
                <div>
                    <div style="display:flex;justify-content:space-between;font-size:0.75rem;color:var(--text-secondary);margin-bottom:0.25rem;">
                        <span>Suspicion score</span><span class="sc-score" style="color:var(--text-primary);font-weight:600;">${score}</span>
                    </div>
                    <div style="height:6px;border-radius:99px;background:rgba(255,255,255,0.08);overflow:hidden;">
                        <div class="sc-meter" style="height:100%;width:${scorePct}%;border-radius:99px;background:var(--${tier.cls});transition:width 0.6s ease, background 0.3s;"></div>
                    </div>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.4rem;font-size:0.72rem;color:var(--text-secondary);">
                    <div>Yaw <span class="sc-yaw" style="color:var(--text-primary);font-variant-numeric:tabular-nums;">${student.yaw}°</span></div>
                    <div>Pitch <span class="sc-pitch" style="color:var(--text-primary);font-variant-numeric:tabular-nums;">${student.pitch}°</span></div>
                    <div>Gaze <span class="sc-gaze" style="color:var(--text-primary);">${student.gaze}</span></div>
                </div>
                <div style="display:flex;justify-content:space-between;font-size:0.75rem;">
                    <span class="sc-status" style="color:var(--text-primary);">${student.status}${calib}</span>
                    <span style="color:var(--text-secondary);">Phone <span class="sc-phone" style="color:var(--text-primary);">${phone}</span></span>
                </div>
                <div class="sc-lastev" style="font-size:0.7rem;color:var(--text-secondary);border-top:1px solid rgba(255,255,255,0.06);padding-top:0.4rem;">${lastEventText}</div>
            `;
            studentsGrid.appendChild(card);
        } else {
            card.querySelector('.sc-avatar').style.background = `var(--${tier.cls})`;
            const tierEl = card.querySelector('.sc-tier');
            tierEl.textContent = tier.label;
            tierEl.style.color = `var(--${tier.cls})`;
            tierEl.style.borderColor = `var(--${tier.cls})`;
            card.querySelector('.sc-score').textContent = score;
            const meter = card.querySelector('.sc-meter');
            meter.style.width = `${scorePct}%`;
            meter.style.background = `var(--${tier.cls})`;
            card.querySelector('.sc-yaw').textContent = `${student.yaw}°`;
            card.querySelector('.sc-pitch').textContent = `${student.pitch}°`;
            card.querySelector('.sc-gaze').textContent = student.gaze;
            card.querySelector('.sc-status').textContent = `${student.status}${calib}`;
            card.querySelector('.sc-phone').textContent = phone;
            card.querySelector('.sc-lastev').textContent = lastEventText;

            if (student.tier === 'HIGH' || student.tier === 'CRITICAL') {
                card.classList.add('danger-pulse');
            } else {
                card.classList.remove('danger-pulse');
            }
        }
    });
}

// Update Room Status Banner
function updateRoomStatus(status, unknownCount, phoneDetected, cameraBlocked) {
    unknownCountBadge.textContent = `${unknownCount} Unknown`;
    const liveDot = document.querySelector('.dot.live');

    function danger(text) {
        globalStatusBanner.style.background = 'rgba(239, 68, 68, 0.2)';
        globalStatusBanner.style.color = 'var(--danger)';
        globalStatusBanner.style.border = '1px solid rgba(239, 68, 68, 0.5)';
        globalStatusText.textContent = text;
        if (liveDot) liveDot.style.background = 'var(--danger)';
    }

    if (cameraBlocked) {
        danger('CRITICAL: CAMERA BLOCKED!');
    } else if (phoneDetected) {
        danger('CRITICAL: CELL PHONE DETECTED!');
    } else if (status === 'UNKNOWN PERSON' || unknownCount > 0) {
        danger('HIGH RISK: UNKNOWN PERSON DETECTED');
    } else {
        globalStatusBanner.style.background = 'rgba(16, 185, 129, 0.1)';
        globalStatusBanner.style.color = 'var(--success)';
        globalStatusBanner.style.border = '1px solid rgba(16, 185, 129, 0.2)';
        globalStatusText.textContent = 'ROOM SECURE';
        if (liveDot) liveDot.style.background = 'var(--success)';
    }
}

// Adds an alert to the Alert Center.
// eventTime: the time the event actually occurred (from the backend). Falls
// back to now for locally generated notices. A forensic log must show when
// the behaviour happened, not when the browser happened to render it.
function addAlert(message, type, eventTime) {
    const time = eventTime || new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
    const alertHtml = `
        <div class="alert-item ${type}">
            <span class="alert-title">${message}</span>
            <span class="alert-time">${time}</span>
        </div>
    `;
    alertsList.insertAdjacentHTML('afterbegin', alertHtml);
    
    if (alertsList.children.length > 20) {
        alertsList.lastElementChild.remove();
    }
}

// Confirmed alerts already shown in the Alert Center (dedupe keys)
const seenAlerts = new Set();

function feedConfirmedAlerts(owner, alerts, tierCls) {
    // The backend sends newest-first. addAlert prepends, so replay
    // oldest-first to leave the newest alert at the top of the list.
    (alerts || []).slice().reverse().forEach(a => {
        const key = `${owner}|${a.type}|${a.time}`;
        if (seenAlerts.has(key)) return;
        seenAlerts.add(key);
        const dur = a.duration ? ` for ${a.duration}s` : '';
        addAlert(`${owner}: ${a.label}${dur} (${a.confidence}% conf, +${a.points})`, tierCls, a.time);
    });
    if (seenAlerts.size > 600) seenAlerts.clear();  // bound memory
}

// Fetch live data from backend
async function fetchLiveStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();

        updateRoomStatus(data.room_status, data.unknown_count, data.phone_detected, data.camera_blocked);
        updateStudentsGrid(data.students);

        // The backend's temporal engine only confirms an alert after the
        // behaviour is sustained/repeated - so everything arriving here is
        // already filtered. Feed each confirmed alert once.
        data.students.forEach(student => {
            const cls = (student.tier === 'HIGH' || student.tier === 'CRITICAL') ? 'danger'
                      : (student.tier === 'MEDIUM' ? 'warning' : 'info');
            feedConfirmedAlerts(student.name || student.id, student.alerts, cls);
        });
        feedConfirmedAlerts('Room', data.room_alerts, 'danger');

    } catch (e) {
        console.error("Error fetching live status:", e);
    }
}

document.addEventListener('DOMContentLoaded', init);

// ─── Toast System ───────────────────────────────────────────
function showToast(message, type = 'success', duration = 3000) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = `
            position:fixed;bottom:2rem;right:2rem;z-index:99999;
            display:flex;flex-direction:column;gap:0.75rem;pointer-events:none;
        `;
        document.body.appendChild(container);
    }
    const icons = { success: '✓', error: '✕', info: 'ℹ', warning: '⚠' };
    const borders = { success: 'rgba(50,215,75,0.4)', error: 'rgba(255,69,58,0.4)', info: 'rgba(59,130,246,0.4)', warning: 'rgba(245,158,11,0.4)' };
    const textColors = { success: '#10b981', error: '#ef4444', info: '#3b82f6', warning: '#f59e0b' };
    const toast = document.createElement('div');
    toast.style.cssText = `
        display:flex;align-items:center;gap:0.75rem;padding:0.875rem 1.25rem;
        border-radius:14px;background:rgba(10,10,12,0.97);backdrop-filter:blur(20px);
        border:1px solid ${borders[type]||borders.info};color:#fff;
        font-family:var(--font-family);font-size:0.875rem;font-weight:500;
        box-shadow:0 8px 32px rgba(0,0,0,0.6);transform:translateX(120%);
        transition:transform 0.35s cubic-bezier(0.16,1,0.3,1);pointer-events:all;
        min-width:220px;max-width:340px;
    `;
    toast.innerHTML = `<span style="font-size:1.1rem;color:${textColors[type]}">${icons[type]}</span><span>${message}</span>`;
    container.appendChild(toast);
    requestAnimationFrame(() => requestAnimationFrame(() => { toast.style.transform = 'translateX(0)'; }));
    setTimeout(() => {
        toast.style.transform = 'translateX(120%)';
        setTimeout(() => toast.remove(), 400);
    }, duration);
}

// ─── Monitoring Page Interactions ───────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // Maximize button → fullscreen
    const maximizeBtn = document.querySelector('.btn-icon[aria-label="Maximize"]');
    if (maximizeBtn) {
        maximizeBtn.addEventListener('click', () => {
            const videoCard = document.getElementById('video-feed');
            if (!videoCard) return;
            const isFullscreen = document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement;
            if (!isFullscreen) {
                if (videoCard.requestFullscreen) videoCard.requestFullscreen();
                else if (videoCard.webkitRequestFullscreen) videoCard.webkitRequestFullscreen();
                else if (videoCard.msRequestFullscreen) videoCard.msRequestFullscreen();
                showToast('Entered fullscreen — press Esc to exit', 'info');
            } else {
                if (document.exitFullscreen) document.exitFullscreen();
                else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
                else if (document.msExitFullscreen) document.msExitFullscreen();
            }
        });
    }

    // Back button logic
    const backBtn = document.getElementById('backBtn');
    if (backBtn) {
        backBtn.addEventListener('click', (e) => {
            if (sessionStorage.getItem('navSource') === 'unified-suite') {
                e.preventDefault();
                sessionStorage.setItem('returningFromSuite', 'true');
                window.location.href = 'index.html';
            }
        });
    }

    // Reports nav link hover
    const navReports = document.getElementById('navReportsBtn');
    if (navReports) {
        navReports.addEventListener('mouseenter', () => {
            navReports.style.color = 'var(--text-primary)';
            navReports.style.borderColor = 'var(--accent)';
            navReports.style.background = 'rgba(59,130,246,0.08)';
        });
        navReports.addEventListener('mouseleave', () => {
            navReports.style.color = 'var(--text-secondary)';
            navReports.style.borderColor = 'var(--card-border)';
            navReports.style.background = 'transparent';
        });
    }

    // Replay nav link hover
    const navReplay = document.getElementById('navReplayBtn');
    if (navReplay) {
        navReplay.addEventListener('mouseenter', () => {
            navReplay.style.color = 'var(--text-primary)';
            navReplay.style.borderColor = 'var(--accent)';
            navReplay.style.background = 'rgba(59,130,246,0.08)';
        });
        navReplay.addEventListener('mouseleave', () => {
            navReplay.style.color = 'var(--text-secondary)';
            navReplay.style.borderColor = 'var(--card-border)';
            navReplay.style.background = 'transparent';
        });
    }

    // User profile click
    const userProfile = document.querySelector('.user-profile');
    if (userProfile) {
        userProfile.style.cursor = 'pointer';
        userProfile.addEventListener('click', () => showToast('Admin Profile — Coming Soon', 'info'));
    }

    // Student avatar click → view details toast
    const studentAvatar = document.getElementById('studentAvatar');
    if (studentAvatar) {
        studentAvatar.style.cursor = 'pointer';
        studentAvatar.addEventListener('click', () => {
            showToast(`Viewing ${currentData.name || 'Student'} profile`, 'info');
        });
    }

    // Trust score card click
    const trustCard = document.getElementById('trust-score');
    if (trustCard) {
        trustCard.style.cursor = 'pointer';
        trustCard.addEventListener('click', () => {
            showToast('Detailed trust analysis — navigating to Reports', 'info', 1500);
            setTimeout(() => { window.location.href = 'reports.html'; }, 1400);
        });
    }

    // Alert on suspicious status change
    const origUpdateStyles = window.updateStyles;
});
