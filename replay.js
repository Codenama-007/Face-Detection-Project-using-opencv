/* ============================================================
   ProctorAI Exam Replay Engine — replay.js
   All controls wired. Export working. Touch support added.
   ============================================================ */

// ─── Toast System ────────────────────────────────────────────
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
    const borders = { success: 'rgba(50,215,75,0.4)', error: 'rgba(255,69,58,0.4)', info: 'rgba(10,132,255,0.4)', warning: 'rgba(255,214,10,0.4)' };
    const textColors = { success: '#32d74b', error: '#ff453a', info: '#0a84ff', warning: '#ffd60a' };
    const toast = document.createElement('div');
    toast.style.cssText = `
        display:flex;align-items:center;gap:0.75rem;padding:0.875rem 1.25rem;
        border-radius:14px;background:rgba(8,8,10,0.97);backdrop-filter:blur(20px);
        border:1px solid ${borders[type]||borders.info};color:#f5f5f7;
        font-family:'Inter',-apple-system,sans-serif;font-size:0.875rem;font-weight:500;
        box-shadow:0 8px 32px rgba(0,0,0,0.7);transform:translateX(120%);
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

// ─── Download Helper ─────────────────────────────────────────
function downloadBlob(content, filename, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 500);
}

// ─── Chart defaults ──────────────────────────────────────────
Chart.defaults.color = '#86868b';
Chart.defaults.font.family = "'Inter', -apple-system, BlinkMacSystemFont, sans-serif";
Chart.defaults.scale.grid.color = 'rgba(255, 255, 255, 0.04)';

const commonChartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: { display: false },
        tooltip: {
            backgroundColor: 'rgba(20,20,22,0.95)', titleColor: '#fff',
            bodyColor: '#e5e7eb', cornerRadius: 10,
            borderColor: 'rgba(255,255,255,0.1)', borderWidth: 1,
            padding: 12, boxPadding: 6, usePointStyle: true
        }
    },
    interaction: { mode: 'index', intersect: false }
};

document.addEventListener('DOMContentLoaded', () => {

    // ── 1. Timeline Events ────────────────────────────────────
    const timelineEvents = [
        { time: '10:10', title: 'Exam Started',        type: 'info',    icon: 'play',          progress: 0 },
        { time: '10:12', title: 'Looking Left',        type: 'warning', icon: 'eye',           progress: 10 },
        { time: '10:14', title: 'Looking Right',       type: 'warning', icon: 'eye',           progress: 20 },
        { time: '10:18', title: 'Face Missing',        type: 'danger',  icon: 'user-minus',    progress: 35 },
        { time: '10:20', title: 'Returned',            type: 'info',    icon: 'user-check',    progress: 45 },
        { time: '10:25', title: 'Risk Increased',      type: 'danger',  icon: 'alert-triangle',progress: 60 },
        { time: '10:30', title: 'Suspicious Activity', type: 'danger',  icon: 'alert-circle',  progress: 75 },
        { time: '10:45', title: 'Exam Submitted',      type: 'info',    icon: 'check-circle',  progress: 100 }
    ];

    const descriptions = {
        'Exam Started':          'Identity verification completed successfully. Environment scan passed. Examination officially commenced.',
        'Looking Left':          'Subject\'s gaze deviated sharply to the left quadrant of the screen boundary for an extended duration (4.2 seconds). This is flagged as a potential off-screen resource reference.',
        'Looking Right':         'Subject frequently glancing to the lower right area off-screen. Pattern suggests reading from a secondary device or notes.',
        'Face Missing':          'Facial tracking completely lost. Subject either moved entirely out of frame or obscured the camera feed.',
        'Returned':              'Face re-acquired by tracking system. Posture normalization detected.',
        'Risk Increased':        'Cumulative anomalous events exceeded threshold. Heuristic risk calculation bumped to critical tier.',
        'Suspicious Activity':   'Multiple audio sources detected (speaking voices) combined with erratic eye movement.',
        'Exam Submitted':        'Final exam payload successfully transmitted and locked by server.'
    };

    // ── DOM refs ──────────────────────────────────────────────
    const timelineTrack   = document.querySelector('.timeline-track');
    const detailTime      = document.querySelector('.detail-time');
    const detailTitle     = document.querySelector('.detail-title');
    const descEl          = document.querySelector('.detail-desc');
    const statusOverlay   = document.querySelector('.status-overlay');
    const timestampOverlay= document.querySelector('.timestamp-overlay');
    const btnPlayPause    = document.getElementById('btnPlayPause');
    const btnPrev         = document.getElementById('btnPrev');
    const btnNext         = document.getElementById('btnNext');
    const btnVolume       = document.getElementById('btnVolume');
    const progressBarBg   = document.getElementById('progressBarBg');
    const progressBarFill = document.getElementById('progressBarFill');
    const progressHandle  = document.getElementById('progressHandle');
    const speedControl    = document.getElementById('speedControl');

    // ── State ─────────────────────────────────────────────────
    let currentIndex   = 3;
    let isPlaying      = false;
    let isMuted        = false;
    let currentProgress= 35;
    let playInterval   = null;
    let playbackSpeed  = 1.0;
    let isDragging     = false;

    // ── Build Timeline DOM ───────────────────────────────────
    const tlElements = [];
    timelineEvents.forEach((ev, idx) => {
        const el = document.createElement('div');
        el.className = `tl-event ${ev.type}`;
        el.innerHTML = `<span class="tl-time">${ev.time}</span><span class="tl-title">${ev.title}</span>`;
        timelineTrack.appendChild(el);
        tlElements.push(el);
        el.addEventListener('click', () => {
            selectEvent(idx);
            setProgress(ev.progress);
        });
    });

    // ── Select Event ─────────────────────────────────────────
    function selectEvent(idx) {
        if (idx < 0 || idx >= timelineEvents.length) return;
        currentIndex = idx;
        const ev = timelineEvents[idx];

        tlElements.forEach(e => e.classList.remove('active'));
        tlElements[idx].classList.add('active');
        tlElements[idx].scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });

        if (detailTime)   detailTime.textContent   = ev.time + ':45';
        if (detailTitle)  detailTitle.textContent  = ev.title;
        if (descEl)       descEl.textContent       = descriptions[ev.title] || 'No description available.';

        if (statusOverlay) {
            statusOverlay.innerHTML = `<i data-lucide="${ev.icon}"></i> ${ev.title}`;
            statusOverlay.className = 'status-overlay ' + (ev.type === 'danger' ? 'danger-bg' : (ev.type === 'warning' ? 'warning-bg' : 'accent-bg'));
        }
        if (timestampOverlay) timestampOverlay.textContent = ev.time + ':45';

        // Update incident log badges
        const detailMeta = document.querySelector('.detail-meta');
        if (detailMeta) {
            const severityMap = { danger: 'badge-danger', warning: 'badge-warning', info: 'badge-outline' };
            const severityLabel = { danger: 'High Severity', warning: 'Medium Severity', info: 'Informational' };
            detailMeta.innerHTML = `
                <span class="badge ${severityMap[ev.type]}">${severityLabel[ev.type]}</span>
                <span class="badge badge-outline">Gaze Tracking</span>
            `;
        }

        lucide.createIcons();
    }

    // Initialize
    selectEvent(currentIndex);

    // ── Progress Bar ─────────────────────────────────────────
    function setProgress(percent) {
        percent = Math.max(0, Math.min(100, percent));
        currentProgress = percent;
        if (progressBarFill) progressBarFill.style.width = `${percent}%`;
        if (progressHandle)  progressHandle.style.left  = `${percent}%`;

        if (!isPlaying) {
            let best = 0;
            timelineEvents.forEach((ev, i) => { if (ev.progress <= percent) best = i; });
            if (best !== currentIndex) selectEvent(best);
        }
    }

    function getProgressFromPointer(clientX) {
        const rect = progressBarBg.getBoundingClientRect();
        const x = clientX - rect.left;
        return (x / rect.width) * 100;
    }

    // ── Play / Pause ──────────────────────────────────────────
    function togglePlay() {
        isPlaying = !isPlaying;
        updatePlayButton();
        if (isPlaying) {
            if (currentProgress >= 100) { setProgress(0); selectEvent(0); }
            playInterval = setInterval(() => {
                if (currentProgress >= 100) {
                    isPlaying = false;
                    updatePlayButton();
                    clearInterval(playInterval);
                    showToast('Replay completed', 'success');
                    return;
                }
                const newPct = currentProgress + (0.5 * playbackSpeed);
                currentProgress = newPct;
                if (progressBarFill) progressBarFill.style.width = `${newPct}%`;
                if (progressHandle)  progressHandle.style.left   = `${newPct}%`;

                let activeIdx = 0;
                timelineEvents.forEach((ev, i) => { if (ev.progress <= newPct) activeIdx = i; });
                if (activeIdx !== currentIndex) selectEvent(activeIdx);
            }, 100);
        } else {
            clearInterval(playInterval);
        }
    }

    function updatePlayButton() {
        if (!btnPlayPause) return;
        btnPlayPause.innerHTML = isPlaying
            ? '<i data-lucide="pause"></i>'
            : '<i data-lucide="play"></i>';
        lucide.createIcons();
    }

    // ── Wire controls ────────────────────────────────────────
    if (btnPlayPause) btnPlayPause.addEventListener('click', togglePlay);

    if (btnPrev) {
        btnPrev.addEventListener('click', () => {
            if (currentIndex > 0) {
                selectEvent(currentIndex - 1);
                setProgress(timelineEvents[currentIndex].progress);
            } else {
                setProgress(0);
                showToast('At the beginning of the replay', 'info', 2000);
            }
        });
    }

    if (btnNext) {
        btnNext.addEventListener('click', () => {
            if (currentIndex < timelineEvents.length - 1) {
                selectEvent(currentIndex + 1);
                setProgress(timelineEvents[currentIndex].progress);
            } else {
                setProgress(100);
                showToast('End of replay reached', 'info', 2000);
            }
        });
    }

    if (btnVolume) {
        btnVolume.addEventListener('click', () => {
            isMuted = !isMuted;
            btnVolume.innerHTML = isMuted
                ? '<i data-lucide="volume-x"></i>'
                : '<i data-lucide="volume-2"></i>';
            btnVolume.style.color = isMuted ? 'var(--danger)' : '';
            lucide.createIcons();
            showToast(isMuted ? 'Audio muted' : 'Audio unmuted', 'info', 1800);
        });
    }

    if (speedControl) {
        speedControl.addEventListener('change', (e) => {
            playbackSpeed = parseFloat(e.target.value);
            showToast(`Playback speed: ${e.target.value}x`, 'info', 1800);
        });
    }

    // ── Progress Bar Mouse & Touch ────────────────────────────
    if (progressBarBg) {
        function startDrag(clientX) {
            isDragging = true;
            setProgress(getProgressFromPointer(clientX));
            if (isPlaying) clearInterval(playInterval);
        }
        function duringDrag(clientX) {
            if (!isDragging) return;
            setProgress(getProgressFromPointer(clientX));
        }
        function endDrag() {
            if (!isDragging) return;
            isDragging = false;
            if (isPlaying) {
                clearInterval(playInterval);
                // Restart interval from current position
                isPlaying = false;
                togglePlay();
            }
        }

        // Mouse
        progressBarBg.addEventListener('mousedown', (e) => startDrag(e.clientX));
        document.addEventListener('mousemove', (e) => duringDrag(e.clientX));
        document.addEventListener('mouseup', endDrag);

        // Touch
        progressBarBg.addEventListener('touchstart', (e) => { startDrag(e.touches[0].clientX); }, { passive: true });
        document.addEventListener('touchmove', (e) => { duringDrag(e.touches[0].clientX); }, { passive: true });
        document.addEventListener('touchend', endDrag);
    }

    // ── Header Prev/Next session buttons ─────────────────────
    const headerBtns = document.querySelectorAll('.replay-controls-header .btn-icon');
    if (headerBtns[0]) {
        headerBtns[0].addEventListener('click', () => showToast('Previous session — Coming Soon', 'info'));
    }
    if (headerBtns[1]) {
        headerBtns[1].addEventListener('click', () => showToast('Next session — Coming Soon', 'info'));
    }

    // ── Export Replay button ──────────────────────────────────
    const exportReplayBtn = document.querySelector('.replay-controls-header .btn-primary');
    if (exportReplayBtn) {
        exportReplayBtn.addEventListener('click', () => {
            exportReplayBtn.classList.add('loading');
            showToast('Preparing export…', 'info', 1500);
            const replayData = {
                sessionId: 'REC-9948271',
                student: { id: 'STU-298314', name: 'Alex Johnson' },
                exam: 'ECON202 Final',
                duration: '35m 12s',
                finalRiskScore: 85,
                finalTrustScore: 15,
                currentPlaybackPosition: `${currentProgress.toFixed(1)}%`,
                events: timelineEvents.map((ev, i) => ({
                    index: i,
                    time: ev.time,
                    title: ev.title,
                    type: ev.type,
                    progress: ev.progress,
                    description: descriptions[ev.title] || ''
                })),
                exportedAt: new Date().toISOString()
            };
            setTimeout(() => {
                downloadBlob(JSON.stringify(replayData, null, 2), 'replay-REC-9948271.json', 'application/json');
                exportReplayBtn.classList.remove('loading');
                showToast('Replay Exported Successfully!', 'success');
            }, 1200);
        });
    }

    // ── Timeline filter badges ────────────────────────────────
    const filterAll     = document.querySelector('.badge-all');
    const filterWarning = document.querySelector('.timeline-filters .badge-warning');
    const filterDanger  = document.querySelector('.timeline-filters .badge-danger');

    function setFilterActive(active) {
        [filterAll, filterWarning, filterDanger].forEach(b => {
            if (!b) return;
            b.style.opacity = '0.4';
            b.style.cursor = 'pointer';
        });
        if (active) { active.style.opacity = '1'; }
    }

    if (filterAll) {
        filterAll.style.cursor = 'pointer';
        filterAll.addEventListener('click', () => {
            setFilterActive(filterAll);
            tlElements.forEach(el => el.style.display = 'flex');
        });
    }
    if (filterWarning) {
        filterWarning.style.cursor = 'pointer';
        filterWarning.addEventListener('click', () => {
            setFilterActive(filterWarning);
            tlElements.forEach((el, i) => {
                el.style.display = (timelineEvents[i].type === 'warning') ? 'flex' : 'none';
            });
        });
    }
    if (filterDanger) {
        filterDanger.style.cursor = 'pointer';
        filterDanger.addEventListener('click', () => {
            setFilterActive(filterDanger);
            tlElements.forEach((el, i) => {
                el.style.display = (timelineEvents[i].type === 'danger') ? 'flex' : 'none';
            });
        });
    }

    // ── Incident items click ──────────────────────────────────
    document.querySelectorAll('.incident-item').forEach(item => {
        item.style.cursor = 'pointer';
        const title = item.querySelector('strong')?.textContent;
        const time  = item.querySelector('span')?.textContent;
        item.addEventListener('click', () => {
            if (title) {
                // Find matching timeline event
                const match = timelineEvents.findIndex(ev =>
                    ev.title.toLowerCase().includes(title.toLowerCase().split(' ')[0].toLowerCase())
                );
                if (match >= 0) {
                    selectEvent(match);
                    setProgress(timelineEvents[match].progress);
                    showToast(`Jumped to: ${timelineEvents[match].title}`, 'info', 2000);
                }
            }
        });
    });

    // ── Footer ────────────────────────────────────────────────
    const footerLeft = document.querySelector('.footer-left');
    if (footerLeft) {
        footerLeft.style.cursor = 'pointer';
        footerLeft.title = 'ProctorAI Enterprise';
        footerLeft.addEventListener('click', () => showToast('ProctorAI Enterprise — Forensic Audit Mode', 'info'));
    }
    document.querySelectorAll('.glass-footer span').forEach(span => {
        if (span.textContent.includes('Encrypted')) {
            span.style.cursor = 'pointer';
            span.addEventListener('click', () => showToast('Session data is end-to-end encrypted', 'success'));
        }
        if (span.textContent.includes('Session ID')) {
            span.style.cursor = 'pointer';
            span.addEventListener('click', () => {
                navigator.clipboard?.writeText('REC-9948271').catch(() => {});
                showToast('Session ID copied to clipboard', 'success');
            });
        }
    });

    // ── 2. Risk History Chart ─────────────────────────────────
    const ctxRisk = document.getElementById('riskHistoryChart').getContext('2d');
    const gradDanger = ctxRisk.createLinearGradient(0, 0, 0, 200);
    gradDanger.addColorStop(0, 'rgba(255,69,58,0.3)');
    gradDanger.addColorStop(1, 'rgba(255,69,58,0.0)');

    const riskChart = new Chart(ctxRisk, {
        type: 'line',
        data: {
            labels: ['10:10','10:15','10:20','10:25','10:30','10:35','10:40','10:45'],
            datasets: [{
                label: 'Risk Score', data: [0,15,60,40,85,90,85,85],
                borderColor: '#ff453a', backgroundColor: gradDanger,
                borderWidth: 2, fill: true, tension: 0.4,
                pointBackgroundColor: '#08080a', pointBorderColor: '#ff453a',
                pointBorderWidth: 2, pointRadius: 4, pointHoverRadius: 6
            }]
        },
        options: {
            ...commonChartOptions,
            scales: {
                x: { display: false, grid: { display: false } },
                y: { beginAtZero: true, max: 100, grid: { drawBorder: false }, ticks: { padding: 10 } }
            }
        }
    });

    // Click on chart to jump to that time
    document.getElementById('riskHistoryChart').addEventListener('click', (e) => {
        const pts = riskChart.getElementsAtEventForMode(e, 'nearest', { intersect: true }, true);
        if (pts.length) {
            const idx = pts[0].index;
            const progress = (idx / 7) * 100;
            setProgress(progress);
            showToast(`Jumped to ${riskChart.data.labels[idx]}`, 'info', 2000);
        }
    });

    // ── 3. Direction Doughnut Chart ───────────────────────────
    const ctxDir = document.getElementById('directionChart').getContext('2d');
    new Chart(ctxDir, {
        type: 'doughnut',
        data: {
            labels: ['CENTER','LEFT','RIGHT','NO_FACE','TOP','BOTTOM'],
            datasets: [{
                data: [45,20,15,10,5,5],
                backgroundColor: ['#32d74b','#ffd60a','#ff9f0a','#ff453a','#0a84ff','#86868b'],
                borderWidth: 0, hoverOffset: 4
            }]
        },
        options: {
            ...commonChartOptions,
            cutout: '75%',
            plugins: {
                legend: { display: true, position: 'right', labels: { color: '#e5e7eb', usePointStyle: true, boxWidth: 8, font: { size: 11 }, padding: 15 } },
                tooltip: {
                    ...commonChartOptions.plugins.tooltip,
                    callbacks: { label: ctx => ` ${ctx.label}: ${ctx.parsed}%` }
                }
            }
        }
    });

    // ── 4. Back Button Logic ─────────────────────────────────
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
});
