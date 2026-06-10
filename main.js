// Sample Data from Backend as per requirements
let currentData = {
    "id": 101,
    "name": "Student Name",
    "risk_score": 0,
    "direction": "CENTER",
    "status": "NORMAL"
};

// DOM Elements
const studentNameEl = document.getElementById('studentName');
const studentIdEl = document.getElementById('studentId');
const studentStatusBadge = document.getElementById('studentStatusBadge');
const riskScoreEl = document.getElementById('riskScore');
const currentStatusEl = document.getElementById('currentStatus');
const gazeDirectionEl = document.getElementById('gazeDirection');
const riskCard = document.getElementById('riskCard');
const statusCard = document.getElementById('statusCard');
const trustScoreProgress = document.getElementById('trustScoreProgress');
const trustScoreValue = document.getElementById('trustScoreValue');
const alertsList = document.getElementById('alertsList');
const timeline = document.getElementById('timeline');
const faceBox = document.getElementById('faceBox');

// Initialize with initial data
function init() {
    updateUI(currentData);
    addTimelineEvent('Monitoring started', 'info');
    
    // Simulate incoming data based on the provided required structure
    setInterval(simulateIncomingData, 3500);
    
    // Animate face box in video feed lightly
    setInterval(animateFaceBox, 1500);
}

// Update the user interface with the new data object
function updateUI(data) {
    // Update Profile
    studentNameEl.textContent = data.name;
    studentIdEl.textContent = data.id;

    // Update Stats
    riskScoreEl.textContent = data.risk_score;
    currentStatusEl.textContent = data.status;
    gazeDirectionEl.textContent = data.direction;

    // Update Trust Score (100 - risk_score) bounded between 0 and 100
    const trustScore = Math.max(0, Math.min(100, 100 - data.risk_score));
    trustScoreValue.textContent = `${trustScore}%`;
    
    // Circular progress stroke dashoffset calculation
    // Circle circumference = 2 * pi * r (r=40 -> 251.2)
    const offset = 251.2 - (251.2 * trustScore) / 100;
    trustScoreProgress.style.strokeDashoffset = offset;

    // Style adjustments based on risk
    updateStyles(data);
}

// Applies dynamic styles/animations depending on the status and risk score
function updateStyles(data) {
    // Reset classes
    riskCard.className = 'card glass-card stat-card';
    statusCard.className = 'card glass-card stat-card';
    trustScoreProgress.style.stroke = 'var(--success)';
    studentStatusBadge.style.background = 'var(--success)';
    studentStatusBadge.textContent = 'Active';

    if (data.status === 'SUSPICIOUS' || data.risk_score >= 25) {
        riskCard.classList.add('danger-pulse');
        statusCard.classList.add('danger-pulse');
        trustScoreProgress.style.stroke = 'var(--danger)';
        studentStatusBadge.style.background = 'var(--danger)';
        studentStatusBadge.textContent = 'Flagged';
        currentStatusEl.style.color = 'var(--danger)';
        riskScoreEl.style.color = 'var(--danger)';
    } else if (data.status === 'WARNING' || data.risk_score > 10) {
        trustScoreProgress.style.stroke = 'var(--warning)';
        studentStatusBadge.style.background = 'var(--warning)';
        currentStatusEl.style.color = 'var(--warning)';
        riskScoreEl.style.color = 'var(--warning)';
    } else {
        currentStatusEl.style.color = 'var(--text-primary)';
        riskScoreEl.style.color = 'var(--text-primary)';
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
    
    // Keep only last 10 alerts
    if (alertsList.children.length > 10) {
        alertsList.lastElementChild.remove();
    }
}

// Adds an event to the Activity Timeline
function addTimelineEvent(message, type) {
    const time = new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
    const dotClass = type === 'danger' ? 'danger' : (type === 'warning' ? 'warning' : '');
    const timelineHtml = `
        <div class="timeline-item">
            <div class="timeline-dot ${dotClass}"></div>
            <div class="timeline-content">
                <span class="timeline-text">${message}</span>
                <span class="timeline-time">${time}</span>
            </div>
        </div>
    `;
    timeline.insertAdjacentHTML('afterbegin', timelineHtml);
    
    // Keep only last 15 events
    if (timeline.children.length > 15) {
        timeline.lastElementChild.remove();
    }
}

// Animates the tracking box in the placeholder video feed
function animateFaceBox() {
    // Generate some jittering coordinates
    let x = 0, y = 0;
    
    if (currentData.direction === 'LEFT') {
        x = -40 - Math.random() * 10;
    } else if (currentData.direction === 'RIGHT') {
        x = 40 + Math.random() * 10;
    } else {
        x = Math.random() * 10 - 5;
    }
    
    y = Math.random() * 10 - 5;

    faceBox.style.transform = `translate(${x}px, ${y}px)`;
    
    if (currentData.status === 'SUSPICIOUS') {
        faceBox.style.borderColor = 'var(--danger)';
    } else if (currentData.status === 'WARNING') {
        faceBox.style.borderColor = 'var(--warning)';
    } else {
        faceBox.style.borderColor = 'var(--success)';
    }
}

// Simulation Logic to showcase dynamic nature of the Dashboard
const scenarios = [
    { risk_score: 5, direction: 'CENTER', status: 'NORMAL' },
    { risk_score: 15, direction: 'RIGHT', status: 'WARNING' },
    { risk_score: 25, direction: 'LEFT', status: 'SUSPICIOUS' }, // Matching prompt request
    { risk_score: 30, direction: 'LEFT', status: 'SUSPICIOUS' },
    { risk_score: 10, direction: 'CENTER', status: 'NORMAL' },
    { risk_score: 0, direction: 'CENTER', status: 'NORMAL' },
];

let simIndex = 0;

function simulateIncomingData() {
    // Cycle through scenarios
    const newData = scenarios[simIndex];
    simIndex = (simIndex + 1) % scenarios.length;
    
    const prevStatus = currentData.status;
    
    // Update the state
    currentData = { ...currentData, ...newData };
    updateUI(currentData);
    
    // Generate meaningful alerts based on state transition
    if (newData.status === 'SUSPICIOUS' && prevStatus !== 'SUSPICIOUS') {
        addAlert(`Suspicious behavior detected: Gaze ${newData.direction}`, 'danger');
        addTimelineEvent(`User gaze fixed ${newData.direction}. Flagged.`, 'danger');
    } else if (newData.status === 'WARNING' && prevStatus === 'NORMAL') {
        addTimelineEvent(`User looking ${newData.direction}`, 'warning');
    } else if (newData.status === 'NORMAL' && prevStatus !== 'NORMAL') {
        addTimelineEvent(`Focus returned to center`, '');
    } else if (newData.status === 'SUSPICIOUS' && prevStatus === 'SUSPICIOUS') {
        // Continuous suspicious
        addTimelineEvent(`Prolonged irregular gaze behavior`, 'danger');
    }
}

// Start execution
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
