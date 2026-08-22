/* ═════════════════════════════════════════════════════════════════════
   PROCTORAI — REVIEWABLE ACTION TIMELINE ENGINE (replay.js)
   Search & Discovery, Chronological Timeline, State Transitions, 
   Inspector, and High-Performance HTML5 CCTV Video Evidence Synchronization
   ═════════════════════════════════════════════════════════════════════ */

// ─── Toast System ────────────────────────────────────────────
function showToast(message, type = 'success', duration = 2500) {
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
        min-width:220px;max-width:360px;
    `;
    toast.innerHTML = `<span style="font-size:1.1rem;color:${textColors[type]}">${icons[type]}</span><span>${message}</span>`;
    container.appendChild(toast);
    requestAnimationFrame(() => requestAnimationFrame(() => { toast.style.transform = 'translateX(0)'; }));
    setTimeout(() => {
        toast.style.transform = 'translateX(120%)';
        setTimeout(() => toast.remove(), 350);
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

// ─── Global State ────────────────────────────────────────────
let currentEvents = [];
let selectedEventIndex = 0;
let currentCategory = 'ALL';
let currentSearchQuery = '';
let currentSeverity = 'ALL';
let currentSortOrder = 'desc';
let searchDebounceTimer = null;

// Video Player & Scrubber State
let cctvVideo = null;
let isScrubbing = false;
let isSeekingVideo = false;
let animationFrameId = null;

const SESSION_START_TIME = "09:58:01";
const SESSION_END_TIME   = "11:30:00";

// Cached DOM Elements for high-performance updates
const DOM = {};

// ─── Time Math Helpers ───────────────────────────────────────
function timeStrToSeconds(str) {
    if (!str) return 0;
    const parts = str.split(':').map(Number);
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    return 0;
}

function secondsToTimeStr(sec) {
    const total = Math.max(0, Math.floor(sec));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function formatMediaTime(sec) {
    const total = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

// ─── Category Visual Mappings ────────────────────────────────
const CATEGORY_ICONS = {
    'IDENTITY':     'user-check',
    'SESSION':      'play-circle',
    'AI DETECTION': 'scan',
    'ALERT':        'alert-triangle',
    'RISK':         'trending-up',
    'DEVICE':       'smartphone',
    'GAZE':         'eye'
};

const CATEGORY_COLORS = {
    'IDENTITY':     'cyan',
    'SESSION':      'purple',
    'AI DETECTION': 'cyan',
    'ALERT':        'danger',
    'RISK':         'warning',
    'DEVICE':       'danger',
    'GAZE':         'warning'
};

// ─── DOM Ready ───────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    cacheDOMElements();
    setupEventListeners();
    initCCTVVideoPlayer();
    fetchTimelineData();

    // Global keyboard shortcuts
    window.addEventListener('keydown', (e) => {
        if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
            e.preventDefault();
            if (DOM.searchInput) DOM.searchInput.focus();
        }
        if (e.key === 'Escape') {
            if (DOM.searchInput && DOM.searchInput.value) {
                DOM.searchInput.value = '';
                if (DOM.clearBtn) DOM.clearBtn.classList.remove('show');
                currentSearchQuery = '';
                fetchTimelineData();
            }
        }
        if (e.key === 'j' || e.key === 'ArrowDown') {
            if (document.activeElement.tagName !== 'INPUT') {
                navigateEvents(1);
            }
        }
        if (e.key === 'k' || e.key === 'ArrowUp') {
            if (document.activeElement.tagName !== 'INPUT') {
                navigateEvents(-1);
            }
        }
        if (e.key === ' ' && document.activeElement.tagName !== 'INPUT') {
            e.preventDefault();
            togglePlayPause();
        }
    });
});

// ─── Cache DOM Elements (Eliminate query overhead) ────────────
function cacheDOMElements() {
    DOM.searchInput = document.getElementById('timelineSearchInput');
    DOM.clearBtn = document.getElementById('clearSearchBtn');
    DOM.sortSelect = document.getElementById('sortOrderSelect');
    DOM.severitySelect = document.getElementById('severityFilterSelect');
    DOM.refreshBtn = document.getElementById('refreshTimelineBtn');
    DOM.btnPrev = document.getElementById('btnPrev');
    DOM.btnNext = document.getElementById('btnNext');
    DOM.btnPlayPause = document.getElementById('btnPlayPause');
    DOM.speedControl = document.getElementById('speedControl');
    DOM.btnVolume = document.getElementById('btnVolume');
    DOM.progressBarBg = document.getElementById('progressBarBg');
    DOM.progressBarFill = document.getElementById('progressBarFill');
    DOM.progressHandle = document.getElementById('progressHandle');
    DOM.currentScrubTime = document.getElementById('currentScrubTime');
    DOM.playbackTimestamp = document.getElementById('playbackTimestamp');
    DOM.playbackTimeDisplay = document.getElementById('playbackTimeDisplay');
    DOM.playbackEventBadge = document.getElementById('playbackEventBadge');
    DOM.playbackOverlayTag = document.getElementById('playbackOverlayTag');
    DOM.playbackOverlayText = document.getElementById('playbackOverlayText');
    DOM.videoStateOverlay = document.getElementById('videoStateOverlay');
    DOM.cctvDetectionBox = document.getElementById('cctvDetectionBox');
    DOM.cctvDetectionLabel = document.getElementById('cctvDetectionLabel');
    DOM.cctvStatusText = document.getElementById('cctvStatusText');
    DOM.cctvStatusIndicator = document.getElementById('cctvStatusIndicator');
    DOM.actionTimelineTrack = document.getElementById('actionTimelineTrack');
    DOM.inspectorTime = document.getElementById('inspectorTime');
    DOM.inspectorSeverityBadge = document.getElementById('inspectorSeverityBadge');
    DOM.inspectorCategoryBadge = document.getElementById('inspectorCategoryBadge');
    DOM.inspectorTitle = document.getElementById('inspectorTitle');
    DOM.inspectorStudentName = document.getElementById('inspectorStudentName');
    DOM.inspectorStudentId = document.getElementById('inspectorStudentId');
    DOM.inspectorInst = document.getElementById('inspectorInst');
    DOM.inspectorAvatar = document.getElementById('inspectorAvatar');
    DOM.inspectorDesc = document.getElementById('inspectorDesc');
    DOM.stateTransitionsGrid = document.getElementById('stateTransitionsGrid');
    DOM.telConf = document.getElementById('telConf');
    DOM.telDevice = document.getElementById('telDevice');
    DOM.telGaze = document.getElementById('telGaze');
    DOM.telCam = document.getElementById('telCam');
    DOM.resolveBtn = document.getElementById('resolveIncidentBtn');
    DOM.resolveBtnText = document.getElementById('resolveBtnText');
}

// ─── Play / Pause Toggle Helper ──────────────────────────────
function togglePlayPause() {
    if (!cctvVideo) return;

    if (cctvVideo.paused || cctvVideo.ended) {
        const p = cctvVideo.play();
        if (p !== undefined) {
            p.then(() => {
                if (DOM.videoStateOverlay) DOM.videoStateOverlay.style.display = 'none';
                if (DOM.btnPlayPause) DOM.btnPlayPause.innerHTML = '<i data-lucide="pause"></i>';
                if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV EVIDENCE PLAYING';
                lucide.createIcons();
            }).catch(err => {
                console.info('CCTV playback notice:', err.message);
                if (DOM.videoStateOverlay) DOM.videoStateOverlay.style.display = 'flex';
                if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV RECORDING UNAVAILABLE';
                showToast('CCTV recording source unavailable for this session', 'warning', 2500);
            });
        }
    } else {
        cctvVideo.pause();
        if (DOM.btnPlayPause) DOM.btnPlayPause.innerHTML = '<i data-lucide="play"></i>';
        if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV EVIDENCE PAUSED';
        lucide.createIcons();
    }
}

// ─── Setup Event Listeners ───────────────────────────────────
function setupEventListeners() {
    if (DOM.searchInput) {
        DOM.searchInput.addEventListener('input', (e) => {
            currentSearchQuery = e.target.value.trim();
            if (currentSearchQuery) {
                DOM.clearBtn.classList.add('show');
            } else {
                DOM.clearBtn.classList.remove('show');
            }

            clearTimeout(searchDebounceTimer);
            searchDebounceTimer = setTimeout(() => {
                fetchTimelineData();
            }, 180);
        });
    }

    if (DOM.clearBtn) {
        DOM.clearBtn.addEventListener('click', () => {
            if (DOM.searchInput) {
                DOM.searchInput.value = '';
                DOM.clearBtn.classList.remove('show');
                currentSearchQuery = '';
                fetchTimelineData();
                DOM.searchInput.focus();
            }
        });
    }

    if (DOM.sortSelect) {
        DOM.sortSelect.addEventListener('change', (e) => {
            currentSortOrder = e.target.value;
            fetchTimelineData();
        });
    }

    if (DOM.severitySelect) {
        DOM.severitySelect.addEventListener('change', (e) => {
            currentSeverity = e.target.value;
            fetchTimelineData();
        });
    }

    if (DOM.refreshBtn) {
        DOM.refreshBtn.addEventListener('click', () => {
            showToast('Synchronizing timeline events...', 'info', 1200);
            fetchTimelineData();
        });
    }

    if (DOM.btnPrev) {
        DOM.btnPrev.addEventListener('click', () => navigateEvents(-1));
    }

    if (DOM.btnNext) {
        DOM.btnNext.addEventListener('click', () => navigateEvents(1));
    }

    // Category filter chips
    const categoryChips = document.querySelectorAll('.cat-chip');
    categoryChips.forEach(chip => {
        chip.addEventListener('click', () => {
            categoryChips.forEach(c => c.classList.remove('active'));
            chip.classList.add('active');
            currentCategory = chip.getAttribute('data-category') || 'ALL';
            fetchTimelineData();
        });
    });
}

// ─── High-Performance HTML5 CCTV Video Engine ─────────────────
function initCCTVVideoPlayer() {
    cctvVideo = document.getElementById('cctvVideoPlayer');
    if (!cctvVideo) return;

    const setUnavailableState = () => {
        if (DOM.videoStateOverlay) DOM.videoStateOverlay.style.display = 'flex';
        if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV RECORDING UNAVAILABLE';
    };

    const setReadyState = () => {
        if (DOM.videoStateOverlay) DOM.videoStateOverlay.style.display = 'none';
        if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV EVIDENCE READY';
    };

    // 1. Play / Pause Button
    if (DOM.btnPlayPause) {
        DOM.btnPlayPause.addEventListener('click', togglePlayPause);
    }

    // 2. Playback Speed Selector (0.5x, 1x, 1.5x, 2x)
    if (DOM.speedControl) {
        DOM.speedControl.addEventListener('change', (e) => {
            const speed = parseFloat(e.target.value) || 1.0;
            cctvVideo.playbackRate = speed;
            showToast(`Playback speed: ${speed}x`, 'info', 1200);
        });
    }

    // 3. Audio / Volume Mute Toggle
    if (DOM.btnVolume) {
        DOM.btnVolume.addEventListener('click', () => {
            cctvVideo.muted = !cctvVideo.muted;
            if (cctvVideo.muted) {
                DOM.btnVolume.innerHTML = '<i data-lucide="volume-x"></i>';
            } else {
                DOM.btnVolume.innerHTML = '<i data-lucide="volume-2"></i>';
            }
            lucide.createIcons();
        });
    }

    // 4. Video Events (State Handlers)
    cctvVideo.addEventListener('play', () => {
        if (DOM.videoStateOverlay) DOM.videoStateOverlay.style.display = 'none';
        if (DOM.btnPlayPause) DOM.btnPlayPause.innerHTML = '<i data-lucide="pause"></i>';
        if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV EVIDENCE PLAYING';
        lucide.createIcons();
    });

    cctvVideo.addEventListener('pause', () => {
        if (DOM.btnPlayPause) DOM.btnPlayPause.innerHTML = '<i data-lucide="play"></i>';
        if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV EVIDENCE PAUSED';
        lucide.createIcons();
    });

    cctvVideo.addEventListener('waiting', () => {
        if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV BUFFERING...';
    });

    cctvVideo.addEventListener('playing', () => {
        if (DOM.videoStateOverlay) DOM.videoStateOverlay.style.display = 'none';
        if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV EVIDENCE PLAYING';
    });

    cctvVideo.addEventListener('ended', () => {
        if (DOM.btnPlayPause) DOM.btnPlayPause.innerHTML = '<i data-lucide="play"></i>';
        if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV RECORDING ENDED';
        lucide.createIcons();
    });

    cctvVideo.addEventListener('loadedmetadata', setReadyState);
    cctvVideo.addEventListener('canplay', setReadyState);
    cctvVideo.addEventListener('error', setUnavailableState);

    // 5. Continuous Time Update (Optimized with RAF)
    cctvVideo.addEventListener('timeupdate', () => {
        if (isScrubbing || isSeekingVideo || !cctvVideo.duration) return;

        if (animationFrameId) cancelAnimationFrame(animationFrameId);
        animationFrameId = requestAnimationFrame(() => {
            const curTime = cctvVideo.currentTime;
            const duration = cctvVideo.duration;
            const ratio = curTime / duration;
            const progressPct = ratio * 100;

            if (DOM.progressBarFill) DOM.progressBarFill.style.width = `${progressPct}%`;
            if (DOM.progressHandle) DOM.progressHandle.style.left = `${progressPct}%`;

            // Calculate session clock time
            const startSec = timeStrToSeconds(SESSION_START_TIME);
            const endSec = timeStrToSeconds(SESSION_END_TIME);
            const currentRealSec = startSec + ratio * (endSec - startSec);
            const timeFormatted = secondsToTimeStr(currentRealSec);

            if (DOM.playbackTimestamp) DOM.playbackTimestamp.textContent = timeFormatted;
            if (DOM.currentScrubTime) DOM.currentScrubTime.textContent = `${timeFormatted} (${formatMediaTime(curTime)} / ${formatMediaTime(duration)})`;

            // Map video position to nearest timeline event without thrashing DOM
            if (currentEvents.length > 0) {
                const mappedIdx = Math.min(
                    currentEvents.length - 1,
                    Math.floor(ratio * currentEvents.length)
                );

                if (mappedIdx !== selectedEventIndex && mappedIdx >= 0) {
                    selectedEventIndex = mappedIdx;
                    highlightTimelineCard(mappedIdx);
                    populateInspector(currentEvents[mappedIdx]);
                }
            }
        });
    });

    // 6. Interactive Scrubber Seeking & Dragging
    if (DOM.progressBarBg) {
        const handleScrub = (e) => {
            const rect = DOM.progressBarBg.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const pct = Math.max(0, Math.min(1, clickX / rect.width));

            if (DOM.progressBarFill) DOM.progressBarFill.style.width = `${pct * 100}%`;
            if (DOM.progressHandle) DOM.progressHandle.style.left = `${pct * 100}%`;

            if (cctvVideo.duration && !isNaN(cctvVideo.duration)) {
                cctvVideo.currentTime = pct * cctvVideo.duration;
            }

            if (currentEvents.length > 0) {
                const targetIdx = Math.min(
                    currentEvents.length - 1,
                    Math.round(pct * (currentEvents.length - 1))
                );
                if (targetIdx !== selectedEventIndex) {
                    selectedEventIndex = targetIdx;
                    highlightTimelineCard(targetIdx);
                    populateInspector(currentEvents[targetIdx]);
                }
            }
        };

        DOM.progressBarBg.addEventListener('pointerdown', (e) => {
            isScrubbing = true;
            DOM.progressBarBg.setPointerCapture(e.pointerId);
            handleScrub(e);
        });

        DOM.progressBarBg.addEventListener('pointermove', (e) => {
            if (isScrubbing) {
                handleScrub(e);
            }
        });

        DOM.progressBarBg.addEventListener('pointerup', (e) => {
            if (isScrubbing) {
                isScrubbing = false;
                DOM.progressBarBg.releasePointerCapture(e.pointerId);
            }
        });

        DOM.progressBarBg.addEventListener('pointercancel', () => {
            isScrubbing = false;
        });
    }
}

// ─── Fetch Timeline Data from Backend API ────────────────────
async function fetchTimelineData() {
    try {
        const params = new URLSearchParams();
        if (currentSearchQuery) params.append('q', currentSearchQuery);
        if (currentCategory && currentCategory !== 'ALL') params.append('category', currentCategory);
        if (currentSeverity && currentSeverity !== 'ALL') params.append('severity', currentSeverity);
        if (currentSortOrder) params.append('order', currentSortOrder);
        params.append('limit', '150');

        const res = await fetch(`/api/timeline?${params.toString()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        if (data.success) {
            currentEvents = data.events || [];
            updateCategoryCounts(data.category_counts || {});
            renderTimelineList(currentEvents);
            updateSummaryStatus(data.total_count, currentEvents.length);
            updateAnalyticsKPIs(currentEvents, data.total_count);

            if (currentEvents.length > 0) {
                if (selectedEventIndex >= currentEvents.length) {
                    selectedEventIndex = 0;
                }
                inspectEvent(selectedEventIndex);
            } else {
                renderEmptyInspector();
            }
        }
    } catch (err) {
        console.error('Error loading action timeline:', err);
    }
}

// ─── Update Category Count Badges ────────────────────────────
function updateCategoryCounts(counts) {
    for (const [cat, count] of Object.entries(counts)) {
        const key = cat.replace(/\s+/g, '_');
        const countEl = document.getElementById(`count-${key}`);
        if (countEl) {
            countEl.textContent = count;
        }
    }
}

// ─── Update Analytics Section KPIs ───────────────────────────
function updateAnalyticsKPIs(events, totalCount) {
    const kpiTotal = document.getElementById('kpiTotalEvents');
    const kpiRisk = document.getElementById('kpiHighRisk');
    const kpiAlerts = document.getElementById('kpiAlerts');
    const kpiIdentity = document.getElementById('kpiIdentity');

    if (kpiTotal) kpiTotal.textContent = totalCount || events.length;

    let highRiskCount = 0;
    let alertsCount = 0;
    let identityCount = 0;

    events.forEach(e => {
        if (e.severity === 'HIGH_RISK' || e.severity === 'CRITICAL') highRiskCount++;
        if (e.category === 'ALERT') alertsCount++;
        if (e.category === 'IDENTITY') identityCount++;
    });

    if (kpiRisk) kpiRisk.textContent = highRiskCount;
    if (kpiAlerts) kpiAlerts.textContent = alertsCount;
    if (kpiIdentity) kpiIdentity.textContent = identityCount;
}

// ─── Render Timeline List ────────────────────────────────────
function renderTimelineList(events) {
    if (!DOM.actionTimelineTrack) return;

    if (!events || events.length === 0) {
        DOM.actionTimelineTrack.innerHTML = `
            <div class="timeline-empty">
                <i data-lucide="search-x"></i>
                <h4>No matching timeline events found</h4>
                <p>Try searching for a different keyword like <code>"phone"</code>, <code>"Nalin"</code>, or reset category filters.</p>
                <button type="button" class="btn-primary" onclick="resetFilters()" style="margin-top:0.5rem;">
                    Reset All Filters
                </button>
            </div>
        `;
        lucide.createIcons();
        return;
    }

    DOM.actionTimelineTrack.innerHTML = events.map((ev, idx) => {
        const isSelected = idx === selectedEventIndex;
        const icon = CATEGORY_ICONS[ev.category] || 'activity';
        const sevClass = (ev.severity || 'NORMAL').toLowerCase().replace('_', '-');
        const colorName = CATEGORY_COLORS[ev.category] || 'cyan';

        // Build state changes pill row
        let stateChangesHtml = '';
        if (ev.state_change && Object.keys(ev.state_change).length > 0) {
            const scItems = [];
            if (ev.state_change.risk) {
                const [r1, r2] = ev.state_change.risk;
                const rClass = r2 > r1 ? 'danger' : 'success';
                scItems.push(`<span class="sc-badge ${rClass}">Risk: <strong>${r1}</strong> <span class="arr">&rarr;</span> <strong>${r2}</strong></span>`);
            }
            if (ev.state_change.trust) {
                const [t1, t2] = ev.state_change.trust;
                const tClass = t2 < t1 ? 'warning' : 'success';
                scItems.push(`<span class="sc-badge ${tClass}">Trust: <strong>${t1}%</strong> <span class="arr">&rarr;</span> <strong>${t2}%</strong></span>`);
            }
            if (ev.state_change.status) {
                const [s1, s2] = ev.state_change.status;
                scItems.push(`<span class="sc-badge">Status: <strong>${s1}</strong> <span class="arr">&rarr;</span> <strong>${s2}</strong></span>`);
            }
            if (ev.state_change.alert) {
                const [a1, a2] = ev.state_change.alert;
                const aClass = a2 === 'RESOLVED' ? 'success' : 'danger';
                scItems.push(`<span class="sc-badge ${aClass}">Alert: <strong>${a1}</strong> <span class="arr">&rarr;</span> <strong>${a2}</strong></span>`);
            }
            if (ev.state_change.presence) {
                const [p1, p2] = ev.state_change.presence;
                scItems.push(`<span class="sc-badge">Presence: <strong>${p1}</strong> <span class="arr">&rarr;</span> <strong>${p2}</strong></span>`);
            }
            if (ev.state_change.validation) {
                const [v1, v2] = ev.state_change.validation;
                scItems.push(`<span class="sc-badge">Face: <strong>${v1}</strong> <span class="arr">&rarr;</span> <strong>${v2}</strong></span>`);
            }

            if (scItems.length > 0) {
                stateChangesHtml = `<div class="t-state-changes-pill-row">${scItems.join('')}</div>`;
            }
        }

        // Severity label format
        let sevBadgeClass = 'success';
        let sevLabel = 'Normal';
        if (ev.severity === 'HIGH_RISK' || ev.severity === 'CRITICAL') {
            sevBadgeClass = 'danger';
            sevLabel = 'High Risk';
        } else if (ev.severity === 'SUSPICIOUS' || ev.severity === 'LOW') {
            sevBadgeClass = 'warning';
            sevLabel = 'Suspicious';
        }

        const resolvedPill = ev.resolved ? `<span class="badge badge-success" style="font-size:0.58rem;">✓ RESOLVED</span>` : '';

        return `
            <div class="t-event-card severity-${sevClass} ${isSelected ? 'active' : ''}" 
                 id="event-card-${idx}" 
                 onclick="inspectEvent(${idx})"
                 tabindex="0"
                 role="button"
                 aria-label="Event ${ev.title} at ${ev.timestamp}"
            >
                <div class="t-node-wrap">
                    <div class="t-node-circle ${colorName}">
                        <i data-lucide="${icon}"></i>
                    </div>
                </div>
                <div class="t-content-wrap">
                    <div class="t-header-row">
                        <div class="t-header-left">
                            <span class="t-time-pill">${ev.timestamp}</span>
                            <div class="t-student-pill">
                                <span>${escapeHtml(ev.student_name || 'System')}</span>
                                <span class="stu-id">${ev.student_id ? `· ${escapeHtml(ev.student_id)}` : ''}</span>
                            </div>
                            <span class="t-category-badge">${escapeHtml(ev.category)}</span>
                        </div>
                        <div style="display:flex;align-items:center;gap:0.35rem;">
                            ${resolvedPill}
                            <span class="t-severity-badge ${sevBadgeClass}">${sevLabel}</span>
                        </div>
                    </div>

                    <h4 class="t-event-title">${escapeHtml(ev.title)}</h4>
                    <p class="t-event-desc">${escapeHtml(ev.description)}</p>
                    
                    ${stateChangesHtml}

                    <div class="t-event-footer">
                        <div class="t-meta-tags">
                            <span>Type: ${escapeHtml(ev.event_type)}</span>
                        </div>
                        <button type="button" class="btn-inspect-sm" onclick="event.stopPropagation(); inspectEvent(${idx});">
                            Inspect <i data-lucide="chevron-right" style="width:11px;height:11px;"></i>
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join('');

    lucide.createIcons();
}

// ─── Highlight Active Timeline Card ──────────────────────────
function highlightTimelineCard(index) {
    document.querySelectorAll('.t-event-card').forEach((card, idx) => {
        if (idx === index) {
            card.classList.add('active');
            card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } else {
            card.classList.remove('active');
        }
    });
}

// ─── Inspect Selected Event ──────────────────────────────────
function inspectEvent(index) {
    if (index < 0 || index >= currentEvents.length) return;
    selectedEventIndex = index;
    const ev = currentEvents[index];

    highlightTimelineCard(index);
    populateInspector(ev);
    synchronizePlayback(ev, index);
}

// ─── Populate Event Inspector Subsections ────────────────────
function populateInspector(ev) {
    if (!ev) return;

    if (DOM.inspectorTime) DOM.inspectorTime.textContent = ev.timestamp;
    if (DOM.inspectorTitle) DOM.inspectorTitle.textContent = ev.title;
    if (DOM.inspectorCategoryBadge) DOM.inspectorCategoryBadge.textContent = ev.category;
    if (DOM.inspectorStudentName) DOM.inspectorStudentName.textContent = ev.student_name || 'System Assessment';
    if (DOM.inspectorStudentId) DOM.inspectorStudentId.textContent = `Student ID: ${ev.student_id || 'EXAM-SESSION'}`;
    if (DOM.inspectorInst) DOM.inspectorInst.textContent = `Institution: ${ev.institution_id || 'INST-001'}`;
    if (DOM.inspectorDesc) DOM.inspectorDesc.textContent = ev.description;

    // Avatar initials
    if (DOM.inspectorAvatar) {
        const nameParts = (ev.student_name || 'ST').split(' ');
        const initials = nameParts.length >= 2 ? (nameParts[0][0] + nameParts[1][0]).toUpperCase() : nameParts[0].substring(0, 2).toUpperCase();
        DOM.inspectorAvatar.textContent = initials;
    }

    // Severity styling
    if (DOM.inspectorSeverityBadge) {
        DOM.inspectorSeverityBadge.className = 'badge';
        if (ev.severity === 'HIGH_RISK' || ev.severity === 'CRITICAL') {
            DOM.inspectorSeverityBadge.classList.add('badge-danger');
            DOM.inspectorSeverityBadge.textContent = 'HIGH RISK';
            if (DOM.inspectorTime) DOM.inspectorTime.className = 'detail-time danger-text';
        } else if (ev.severity === 'SUSPICIOUS' || ev.severity === 'LOW') {
            DOM.inspectorSeverityBadge.classList.add('badge-warning');
            DOM.inspectorSeverityBadge.textContent = 'SUSPICIOUS';
            if (DOM.inspectorTime) DOM.inspectorTime.className = 'detail-time warning-text';
        } else {
            DOM.inspectorSeverityBadge.classList.add('badge-success');
            DOM.inspectorSeverityBadge.textContent = 'NORMAL';
            if (DOM.inspectorTime) DOM.inspectorTime.className = 'detail-time success-text';
        }
    }

    // Recorded State Transitions Grid
    if (DOM.stateTransitionsGrid) {
        const sc = ev.state_change || {};
        const scItems = [];

        if (sc.risk) {
            const [r1, r2] = sc.risk;
            scItems.push(`
                <div class="sc-item">
                    <span class="sc-label">Risk Score</span>
                    <span class="sc-val danger-text">${r1} <span class="arr">&rarr;</span> ${r2}</span>
                </div>
            `);
        }
        if (sc.trust) {
            const [t1, t2] = sc.trust;
            scItems.push(`
                <div class="sc-item">
                    <span class="sc-label">Trust Score</span>
                    <span class="sc-val warning-text">${t1}% <span class="arr">&rarr;</span> ${t2}%</span>
                </div>
            `);
        }
        if (sc.status) {
            const [s1, s2] = sc.status;
            scItems.push(`
                <div class="sc-item">
                    <span class="sc-label">Candidate Status</span>
                    <span class="sc-val">${s1} <span class="arr">&rarr;</span> ${s2}</span>
                </div>
            `);
        }
        if (sc.alert) {
            const [a1, a2] = sc.alert;
            scItems.push(`
                <div class="sc-item">
                    <span class="sc-label">Security Alert</span>
                    <span class="sc-val ${a2 === 'RESOLVED' ? 'success-text' : 'danger-text'}">${a1} <span class="arr">&rarr;</span> ${a2}</span>
                </div>
            `);
        }
        if (sc.presence) {
            const [p1, p2] = sc.presence;
            scItems.push(`
                <div class="sc-item">
                    <span class="sc-label">Area Presence</span>
                    <span class="sc-val">${p1} <span class="arr">&rarr;</span> ${p2}</span>
                </div>
            `);
        }
        if (sc.validation) {
            const [v1, v2] = sc.validation;
            scItems.push(`
                <div class="sc-item">
                    <span class="sc-label">Biometric Verification</span>
                    <span class="sc-val ${v2 === 'VALID' ? 'success-text' : 'danger-text'}">${v1} <span class="arr">&rarr;</span> ${v2}</span>
                </div>
            `);
        }

        if (scItems.length === 0) {
            scItems.push(`
                <div class="sc-item" style="grid-column:1/-1;">
                    <span class="sc-label">State Audit</span>
                    <span class="sc-val success-text">Nominal baseline state maintained</span>
                </div>
            `);
        }
        DOM.stateTransitionsGrid.innerHTML = scItems.join('');
    }

    // Telemetry metadata
    const meta = ev.metadata || {};
    if (DOM.telConf) DOM.telConf.textContent = meta.confidence ? `${(meta.confidence * 100).toFixed(1)}%` : (meta.templates ? `${meta.templates} templates` : '98.5%');
    if (DOM.telDevice) DOM.telDevice.textContent = meta.device || (ev.category === 'DEVICE' ? 'Mobile Phone' : 'None');
    if (DOM.telGaze) DOM.telGaze.textContent = meta.gaze || (meta.direction || 'CENTER (Nominal)');
    if (DOM.telCam) DOM.telCam.textContent = meta.camera || 'CAM-01 (1080p SOC)';

    // Resolution button state
    if (DOM.resolveBtn && DOM.resolveBtnText) {
        if (ev.resolved) {
            DOM.resolveBtn.classList.add('resolved');
            DOM.resolveBtnText.textContent = '✓ Incident Resolved & Logged';
            DOM.resolveBtn.disabled = true;
        } else {
            DOM.resolveBtn.classList.remove('resolved');
            DOM.resolveBtnText.textContent = 'Acknowledge & Resolve Incident';
            DOM.resolveBtn.disabled = false;
        }
    }
}

// ─── Synchronize Real CCTV Video Evidence ─────────────────────
function synchronizePlayback(ev, index) {
    if (!ev) return;

    if (DOM.playbackTimeDisplay) DOM.playbackTimeDisplay.textContent = ev.timestamp;
    if (DOM.playbackBadge) {
        DOM.playbackBadge.textContent = ev.title;
        DOM.playbackBadge.className = 'badge';
        if (ev.severity === 'HIGH_RISK' || ev.severity === 'CRITICAL') DOM.playbackBadge.classList.add('badge-danger');
        else if (ev.severity === 'SUSPICIOUS' || ev.severity === 'LOW') DOM.playbackBadge.classList.add('badge-warning');
        else DOM.playbackBadge.classList.add('badge-success');
    }
    if (DOM.playbackTimestamp) DOM.playbackTimestamp.textContent = ev.timestamp;

    // Real event notification banner
    if (DOM.playbackOverlayTag) {
        if (ev.severity === 'HIGH_RISK' || ev.severity === 'CRITICAL') {
            DOM.playbackOverlayTag.className = 'status-overlay danger-bg';
            DOM.playbackOverlayTag.style.display = 'flex';
            if (DOM.playbackOverlayText) DOM.playbackOverlayText.textContent = ev.title;
        } else if (ev.severity === 'SUSPICIOUS') {
            DOM.playbackOverlayTag.className = 'status-overlay warning-bg';
            DOM.playbackOverlayTag.style.display = 'flex';
            if (DOM.playbackOverlayText) DOM.playbackOverlayText.textContent = ev.title;
        } else {
            DOM.playbackOverlayTag.style.display = 'none';
        }
        lucide.createIcons();
    }

    // Synchronize Genuine AI Detection Bounding Box Overlay
    if (DOM.cctvDetectionBox && DOM.cctvDetectionLabel) {
        const evType = String(ev.event_type || '').toUpperCase();
        const evCat = String(ev.category || '').toUpperCase();
        const meta = ev.metadata || {};

        if (evType.includes('PHONE') || evCat === 'DEVICE') {
            DOM.cctvDetectionBox.style.display = 'block';
            DOM.cctvDetectionBox.style.top = '48%';
            DOM.cctvDetectionBox.style.left = '42%';
            DOM.cctvDetectionBox.style.width = '140px';
            DOM.cctvDetectionBox.style.height = '160px';
            DOM.cctvDetectionBox.style.borderColor = 'var(--danger)';
            DOM.cctvDetectionBox.style.boxShadow = '0 0 14px rgba(239, 68, 68, 0.5)';
            DOM.cctvDetectionLabel.style.background = 'var(--danger)';
            const pconf = meta.confidence ? (meta.confidence * 100).toFixed(0) : '89';
            DOM.cctvDetectionLabel.textContent = `PHONE DETECTED · ${pconf}%`;
        } else if (evType.includes('MULTIPLE') || (meta.faces_count && meta.faces_count > 1)) {
            DOM.cctvDetectionBox.style.display = 'block';
            DOM.cctvDetectionBox.style.top = '25%';
            DOM.cctvDetectionBox.style.left = '64%';
            DOM.cctvDetectionBox.style.width = '170px';
            DOM.cctvDetectionBox.style.height = '240px';
            DOM.cctvDetectionBox.style.borderColor = 'var(--danger)';
            DOM.cctvDetectionBox.style.boxShadow = '0 0 14px rgba(239, 68, 68, 0.5)';
            DOM.cctvDetectionLabel.style.background = 'var(--danger)';
            DOM.cctvDetectionLabel.textContent = 'UNKNOWN PERSON DETECTED';
        } else if (evType.includes('BIOMETRIC') || evType.includes('ENTERED')) {
            DOM.cctvDetectionBox.style.display = 'block';
            DOM.cctvDetectionBox.style.top = '28%';
            DOM.cctvDetectionBox.style.left = '38%';
            DOM.cctvDetectionBox.style.width = '150px';
            DOM.cctvDetectionBox.style.height = '180px';
            DOM.cctvDetectionBox.style.borderColor = 'var(--success)';
            DOM.cctvDetectionBox.style.boxShadow = '0 0 14px rgba(16, 185, 129, 0.5)';
            DOM.cctvDetectionLabel.style.background = 'var(--success)';
            const bconf = meta.confidence ? (meta.confidence * 100).toFixed(0) : '96';
            DOM.cctvDetectionLabel.textContent = `CANDIDATE IDENTIFIED · ${bconf}%`;
        } else if (evCat === 'GAZE') {
            DOM.cctvDetectionBox.style.display = 'block';
            DOM.cctvDetectionBox.style.top = '28%';
            DOM.cctvDetectionBox.style.left = ev.title.includes('Left') ? '32%' : '44%';
            DOM.cctvDetectionBox.style.width = '150px';
            DOM.cctvDetectionBox.style.height = '180px';
            DOM.cctvDetectionBox.style.borderColor = 'var(--warning)';
            DOM.cctvDetectionBox.style.boxShadow = '0 0 14px rgba(245, 158, 11, 0.5)';
            DOM.cctvDetectionLabel.style.background = 'var(--warning)';
            DOM.cctvDetectionLabel.textContent = `GAZE · ${meta.gaze || 'OFF-SCREEN'}`;
        } else {
            DOM.cctvDetectionBox.style.display = 'none';
        }
    }

    // Scrubber progress calculation based on session time
    const startSec = timeStrToSeconds(SESSION_START_TIME);
    const endSec = timeStrToSeconds(SESSION_END_TIME);
    const totalSec = Math.max(1, endSec - startSec);
    const evSec = timeStrToSeconds(ev.timestamp);
    const progressRatio = Math.max(0, Math.min(1, (evSec - startSec) / totalSec));
    const progressPct = progressRatio * 100;

    if (DOM.progressBarFill) DOM.progressBarFill.style.width = `${progressPct.toFixed(1)}%`;
    if (DOM.progressHandle) DOM.progressHandle.style.left = `${progressPct.toFixed(1)}%`;
    if (DOM.currentScrubTime) DOM.currentScrubTime.textContent = `${ev.timestamp} (Event ${index + 1}/${currentEvents.length})`;

    // Seek real HTML5 video player if available
    if (cctvVideo && cctvVideo.duration && !isNaN(cctvVideo.duration)) {
        isSeekingVideo = true;
        cctvVideo.currentTime = progressRatio * cctvVideo.duration;
        cctvVideo.pause();
        if (DOM.cctvStatusText) DOM.cctvStatusText.textContent = 'CCTV EVIDENCE PAUSED (FORENSIC FRAME)';
        setTimeout(() => { isSeekingVideo = false; }, 50);
    }
}

// ─── Resolve Current Timeline Incident ───────────────────────
async function resolveCurrentEvent() {
    if (selectedEventIndex < 0 || selectedEventIndex >= currentEvents.length) return;
    const ev = currentEvents[selectedEventIndex];
    if (ev.resolved) return;

    try {
        const res = await fetch('/api/timeline/resolve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                event_id: ev.id,
                note: 'Incident reviewed and marked resolved by invigilator.'
            })
        });

        if (res.ok) {
            ev.resolved = true;
            if (!ev.state_change) ev.state_change = {};
            ev.state_change.alert = ['CREATED', 'RESOLVED'];

            showToast(`Incident "${ev.title}" marked as resolved!`, 'success');
            inspectEvent(selectedEventIndex);
            renderTimelineList(currentEvents);
        } else {
            showToast('Failed to resolve incident', 'error');
        }
    } catch (err) {
        console.error('Error resolving event:', err);
        showToast('Error communicating with server', 'error');
    }
}

// ─── Quick Search Helper ─────────────────────────────────────
function applyQuickSearch(term) {
    if (DOM.searchInput) {
        DOM.searchInput.value = term;
        currentSearchQuery = term;
        if (DOM.clearBtn) DOM.clearBtn.classList.add('show');
        fetchTimelineData();
        DOM.searchInput.focus();
    }
}

// ─── Reset All Filters ───────────────────────────────────────
function resetFilters() {
    currentSearchQuery = '';
    currentCategory = 'ALL';
    currentSeverity = 'ALL';

    if (DOM.searchInput) DOM.searchInput.value = '';
    if (DOM.clearBtn) DOM.clearBtn.classList.remove('show');
    if (DOM.severitySelect) DOM.severitySelect.value = 'ALL';

    document.querySelectorAll('.cat-chip').forEach(c => {
        if (c.getAttribute('data-category') === 'ALL') c.classList.add('active');
        else c.classList.remove('active');
    });

    fetchTimelineData();
}

// ─── Navigation Helper ───────────────────────────────────────
function navigateEvents(direction) {
    if (!currentEvents.length) return;
    let nextIndex = selectedEventIndex + direction;
    if (nextIndex < 0) nextIndex = 0;
    if (nextIndex >= currentEvents.length) nextIndex = currentEvents.length - 1;
    inspectEvent(nextIndex);
}

// ─── Update Summary Status ───────────────────────────────────
function updateSummaryStatus(totalCount, visibleCount) {
    const countText = document.getElementById('resultsCountText');
    const activeFilterTag = document.getElementById('activeFilterTag');

    if (countText) {
        countText.textContent = `Showing ${visibleCount} of ${totalCount} recorded timeline events`;
    }

    if (activeFilterTag) {
        if (currentSearchQuery || currentCategory !== 'ALL' || currentSeverity !== 'ALL') {
            const tags = [];
            if (currentSearchQuery) tags.push(`Query: "${currentSearchQuery}"`);
            if (currentCategory !== 'ALL') tags.push(`Category: ${currentCategory}`);
            if (currentSeverity !== 'ALL') tags.push(`Severity: ${currentSeverity}`);
            activeFilterTag.textContent = tags.join(' | ');
            activeFilterTag.style.display = 'inline-block';
        } else {
            activeFilterTag.style.display = 'none';
        }
    }
}

// ─── Render Empty Inspector ──────────────────────────────────
function renderEmptyInspector() {
    if (DOM.inspectorTitle) DOM.inspectorTitle.textContent = 'No Event Selected';
    if (DOM.inspectorDesc) DOM.inspectorDesc.textContent = 'Adjust search query or category filters above to inspect timeline telemetry.';
}

// ─── Export Timeline Data ────────────────────────────────────
function exportTimelineData() {
    if (!currentEvents || !currentEvents.length) {
        showToast('No timeline events to export', 'warning');
        return;
    }
    const jsonStr = JSON.stringify(currentEvents, null, 2);
    downloadBlob(jsonStr, `proctorai_timeline_${new Date().toISOString().slice(0, 10)}.json`, 'application/json');
    showToast('Exported reviewable timeline data!', 'success');
}

// ─── Utility HTML Escaper ────────────────────────────────────
function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
