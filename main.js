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
    setInterval(fetchLiveStatus, 1000);
}

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

    // Remove the waiting message if it exists
    if (studentsGrid.innerHTML.includes('Waiting for students')) {
        studentsGrid.innerHTML = '';
    }

    // Update or create cards
    students.forEach(student => {
        let card = document.getElementById(`student-card-${student.id}`);
        
        // Calculate trust score
        const trustScore = Math.max(0, Math.min(100, 100 - student.risk_score));
        let riskClass = 'success';
        if (student.risk_score >= 25) riskClass = 'danger';
        else if (student.risk_score >= 10) riskClass = 'warning';

        if (!card) {
            // Create new card
            addAlert(`Identified: ${student.name} (${student.id})`, 'success');
            displayedStudents.add(student.id);
            card = document.createElement('div');
            card.id = `student-card-${student.id}`;
            card.className = `card glass-card stat-card`;
            card.style.display = 'flex';
            card.style.justifyContent = 'space-between';
            card.style.alignItems = 'center';
            card.style.padding = '1rem';
            
            card.innerHTML = `
                <div style="display:flex; align-items:center; gap:1rem;">
                    <div style="width:40px;height:40px;border-radius:50%;background:var(--${riskClass});display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;">
                        ${student.name.charAt(0)}
                    </div>
                    <div>
                        <h4 style="margin:0;font-size:1rem;color:var(--text-primary);">${student.name}</h4>
                        <p style="margin:0;font-size:0.8rem;color:var(--text-secondary);">${student.id}</p>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:1.2rem;font-weight:bold;color:var(--${riskClass});" class="trust-val">${trustScore}% Trust</div>
                    <div style="font-size:0.75rem;color:var(--text-secondary);" class="status-val">${student.status}</div>
                </div>
            `;
            studentsGrid.appendChild(card);
        } else {
            // Update existing card
            const avatar = card.querySelector('div[style*="border-radius:50%"]');
            const trustVal = card.querySelector('.trust-val');
            const statusVal = card.querySelector('.status-val');

            avatar.style.background = `var(--${riskClass})`;
            trustVal.style.color = `var(--${riskClass})`;
            trustVal.textContent = `${trustScore}% Trust`;
            statusVal.textContent = student.status;
            
            if (riskClass === 'danger') {
                card.classList.add('danger-pulse');
            } else {
                card.classList.remove('danger-pulse');
            }
        }
    });
}

// Update Room Status Banner
function updateRoomStatus(status, unknownCount) {
    unknownCountBadge.textContent = `${unknownCount} Unknown`;
    
    if (status === 'HIGH RISK' || unknownCount > 0) {
        globalStatusBanner.style.background = 'rgba(239, 68, 68, 0.2)';
        globalStatusBanner.style.color = 'var(--danger)';
        globalStatusBanner.style.border = '1px solid rgba(239, 68, 68, 0.5)';
        globalStatusText.textContent = 'HIGH RISK: UNKNOWN PERSON DETECTED';
        const liveDot = document.querySelector('.dot.live');
        if (liveDot) liveDot.style.background = 'var(--danger)';
    } else {
        globalStatusBanner.style.background = 'rgba(16, 185, 129, 0.1)';
        globalStatusBanner.style.color = 'var(--success)';
        globalStatusBanner.style.border = '1px solid rgba(16, 185, 129, 0.2)';
        globalStatusText.textContent = 'ROOM SECURE';
        const liveDot = document.querySelector('.dot.live');
        if (liveDot) liveDot.style.background = 'var(--success)';
    }
}

// Adds an alert to the Alert Center
function addAlert(message, type) {
    const time = new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
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

// Fetch live data from backend
async function fetchLiveStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();
        
        updateRoomStatus(data.room_status, data.unknown_count);
        updateStudentsGrid(data.students);
        
        // Check for state transitions to generate alerts
        if (data.unknown_count > 0 && !previousAlerts.includes('unknown_alert')) {
            addAlert('Critical: Unknown person entered the room!', 'danger');
            previousAlerts.push('unknown_alert');
        } else if (data.unknown_count === 0 && previousAlerts.includes('unknown_alert')) {
            addAlert('Room secure. Unknown person left.', 'success');
            previousAlerts = previousAlerts.filter(a => a !== 'unknown_alert');
        }

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
