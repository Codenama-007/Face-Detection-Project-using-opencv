/* ═════════════════════════════════════════════════════════════════════
   PROCTORAI — REVIEWABLE ACTION TIMELINE ENGINE (replay.js)
   Search & Discovery, Chronological Timeline, State Transitions, Inspector
   ═════════════════════════════════════════════════════════════════════ */

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
        min-width:220px;max-width:360px;
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

// ─── Global State ────────────────────────────────────────────
let currentEvents = [];
let selectedEventIndex = 0;
let currentCategory = 'ALL';
let currentSearchQuery = '';
let currentSeverity = 'ALL';
let currentSortOrder = 'desc';
let searchDebounceTimer = null;

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
    setupEventListeners();
    fetchTimelineData();

    // Global keyboard shortcuts
    window.addEventListener('keydown', (e) => {
        if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
            e.preventDefault();
            const searchInput = document.getElementById('timelineSearchInput');
            if (searchInput) searchInput.focus();
        }
        if (e.key === 'Escape') {
            const searchInput = document.getElementById('timelineSearchInput');
            if (searchInput && searchInput.value) {
                searchInput.value = '';
                document.getElementById('clearSearchBtn').classList.remove('show');
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
    });
});

// ─── Setup Event Listeners ───────────────────────────────────
function setupEventListeners() {
    const searchInput = document.getElementById('timelineSearchInput');
    const clearBtn = document.getElementById('clearSearchBtn');
    const sortSelect = document.getElementById('sortOrderSelect');
    const severitySelect = document.getElementById('severityFilterSelect');
    const refreshBtn = document.getElementById('refreshTimelineBtn');
    const btnPrev = document.getElementById('btnPrev');
    const btnNext = document.getElementById('btnNext');

    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            currentSearchQuery = e.target.value.trim();
            if (currentSearchQuery) {
                clearBtn.classList.add('show');
            } else {
                clearBtn.classList.remove('show');
            }

            clearTimeout(searchDebounceTimer);
            searchDebounceTimer = setTimeout(() => {
                fetchTimelineData();
            }, 180);
        });
    }

    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            if (searchInput) {
                searchInput.value = '';
                clearBtn.classList.remove('show');
                currentSearchQuery = '';
                fetchTimelineData();
                searchInput.focus();
            }
        });
    }

    if (sortSelect) {
        sortSelect.addEventListener('change', (e) => {
            currentSortOrder = e.target.value;
            fetchTimelineData();
        });
    }

    if (severitySelect) {
        severitySelect.addEventListener('change', (e) => {
            currentSeverity = e.target.value;
            fetchTimelineData();
        });
    }

    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            showToast('Synchronizing timeline events...', 'info', 1500);
            fetchTimelineData();
        });
    }

    if (btnPrev) {
        btnPrev.addEventListener('click', () => navigateEvents(-1));
    }

    if (btnNext) {
        btnNext.addEventListener('click', () => navigateEvents(1));
    }

    // Category chips
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
        renderTimelineError();
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

// ─── Render Timeline List ────────────────────────────────────
function renderTimelineList(events) {
    const track = document.getElementById('actionTimelineTrack');
    if (!track) return;

    if (!events || events.length === 0) {
        track.innerHTML = `
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

    track.innerHTML = events.map((ev, idx) => {
        const isSelected = idx === selectedEventIndex;
        const icon = CATEGORY_ICONS[ev.category] || 'activity';
        const sevClass = (ev.severity || 'NORMAL').toLowerCase().replace('_', '-');
        const colorName = CATEGORY_COLORS[ev.category] || 'cyan';

        // Build state changes pill row (e.g. Risk: 35 → 60)
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

        const resolvedPill = ev.resolved ? `<span class="badge badge-success" style="font-size:0.6rem;">✓ RESOLVED</span>` : '';

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
                                <span class="stu-id">${ev.student_id ? `(${escapeHtml(ev.student_id)})` : ''}</span>
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
                            <span>•</span>
                            <span>Tenant: ${escapeHtml(ev.institution_id || 'INST-001')}</span>
                        </div>
                        <button type="button" class="btn-inspect-sm" onclick="event.stopPropagation(); inspectEvent(${idx});">
                            Inspect <i data-lucide="chevron-right" style="width:12px;height:12px;"></i>
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join('');

    lucide.createIcons();
}

// ─── Inspect Selected Event (Section 3) ──────────────────────
function inspectEvent(index) {
    if (index < 0 || index >= currentEvents.length) return;
    selectedEventIndex = index;
    const ev = currentEvents[index];

    // Highlight card in timeline
    document.querySelectorAll('.t-event-card').forEach((card, idx) => {
        if (idx === index) {
            card.classList.add('active');
            card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } else {
            card.classList.remove('active');
        }
    });

    // Populate Inspector Fields
    const inspectorTime = document.getElementById('inspectorTime');
    const inspectorSeverityBadge = document.getElementById('inspectorSeverityBadge');
    const inspectorCategoryBadge = document.getElementById('inspectorCategoryBadge');
    const inspectorTitle = document.getElementById('inspectorTitle');
    const inspectorStudentName = document.getElementById('inspectorStudentName');
    const inspectorStudentId = document.getElementById('inspectorStudentId');
    const inspectorInst = document.getElementById('inspectorInst');
    const inspectorAvatar = document.getElementById('inspectorAvatar');
    const inspectorDesc = document.getElementById('inspectorDesc');
    const stateTransitionsGrid = document.getElementById('stateTransitionsGrid');
    const telConf = document.getElementById('telConf');
    const telDevice = document.getElementById('telDevice');
    const telGaze = document.getElementById('telGaze');
    const telCam = document.getElementById('telCam');
    const resolveBtn = document.getElementById('resolveIncidentBtn');
    const resolveBtnText = document.getElementById('resolveBtnText');

    if (inspectorTime) inspectorTime.textContent = ev.timestamp;
    if (inspectorTitle) inspectorTitle.textContent = ev.title;
    if (inspectorCategoryBadge) inspectorCategoryBadge.textContent = ev.category;
    if (inspectorStudentName) inspectorStudentName.textContent = ev.student_name || 'System Assessment';
    if (inspectorStudentId) inspectorStudentId.textContent = `Student ID: ${ev.student_id || 'EXAM-SESSION'}`;
    if (inspectorInst) inspectorInst.textContent = `Institution: ${ev.institution_id || 'INST-001'}`;
    if (inspectorDesc) inspectorDesc.textContent = ev.description;

    // Avatar initials
    if (inspectorAvatar) {
        const nameParts = (ev.student_name || 'ST').split(' ');
        const initials = nameParts.length >= 2 ? (nameParts[0][0] + nameParts[1][0]).toUpperCase() : nameParts[0].substring(0, 2).toUpperCase();
        inspectorAvatar.textContent = initials;
    }

    // Severity styling
    if (inspectorSeverityBadge) {
        inspectorSeverityBadge.className = 'badge';
        if (ev.severity === 'HIGH_RISK' || ev.severity === 'CRITICAL') {
            inspectorSeverityBadge.classList.add('badge-danger');
            inspectorSeverityBadge.textContent = 'HIGH RISK';
            if (inspectorTime) inspectorTime.className = 'detail-time danger-text';
        } else if (ev.severity === 'SUSPICIOUS' || ev.severity === 'LOW') {
            inspectorSeverityBadge.classList.add('badge-warning');
            inspectorSeverityBadge.textContent = 'SUSPICIOUS';
            if (inspectorTime) inspectorTime.className = 'detail-time warning-text';
        } else {
            inspectorSeverityBadge.classList.add('badge-success');
            inspectorSeverityBadge.textContent = 'NORMAL';
            if (inspectorTime) inspectorTime.className = 'detail-time success-text';
        }
    }

    // Recorded State Transitions Grid
    if (stateTransitionsGrid) {
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
        stateTransitionsGrid.innerHTML = scItems.join('');
    }

    // Telemetry metadata
    const meta = ev.metadata || {};
    if (telConf) telConf.textContent = meta.confidence ? `${(meta.confidence * 100).toFixed(1)}%` : (meta.templates ? `${meta.templates} templates` : '98.5%');
    if (telDevice) telDevice.textContent = meta.device || (ev.category === 'DEVICE' ? 'Mobile Phone' : 'None');
    if (telGaze) telGaze.textContent = meta.gaze || (meta.direction || 'CENTER (Nominal)');
    if (telCam) telCam.textContent = meta.camera || 'CAM-01 (1080p SOC)';

    // Resolution button state
    if (resolveBtn && resolveBtnText) {
        if (ev.resolved) {
            resolveBtn.classList.add('resolved');
            resolveBtnText.textContent = '✓ Incident Resolved & Logged';
            resolveBtn.disabled = true;
        } else {
            resolveBtn.classList.remove('resolved');
            resolveBtnText.textContent = 'Acknowledge & Resolve Incident';
            resolveBtn.disabled = false;
        }
    }

    // Synchronize CCTV Evidence Review (Section 4)
    synchronizePlayback(ev, index);
}

// ─── Synchronize CCTV Evidence (Section 4) ───────────────────
function synchronizePlayback(ev, index) {
    const playbackTime = document.getElementById('playbackTimeDisplay');
    const playbackBadge = document.getElementById('playbackEventBadge');
    const playbackTimestamp = document.getElementById('playbackTimestamp');
    const playbackOverlayTag = document.getElementById('playbackOverlayTag');
    const playbackFaceBox = document.getElementById('playbackFaceBox');
    const progressBarFill = document.getElementById('progressBarFill');
    const progressHandle = document.getElementById('progressHandle');
    const currentScrubTime = document.getElementById('currentScrubTime');

    if (playbackTime) playbackTime.textContent = ev.timestamp;
    if (playbackBadge) {
        playbackBadge.textContent = ev.title;
        playbackBadge.className = 'badge';
        if (ev.severity === 'HIGH_RISK') playbackBadge.classList.add('badge-danger');
        else if (ev.severity === 'SUSPICIOUS') playbackBadge.classList.add('badge-warning');
        else playbackBadge.classList.add('badge-success');
    }
    if (playbackTimestamp) playbackTimestamp.textContent = ev.timestamp;
    if (playbackOverlayTag) {
        playbackOverlayTag.className = 'status-overlay';
        if (ev.severity === 'HIGH_RISK') {
            playbackOverlayTag.classList.add('danger-bg');
            playbackOverlayTag.innerHTML = `<i data-lucide="alert-triangle"></i> ${escapeHtml(ev.title)}`;
        } else if (ev.severity === 'SUSPICIOUS') {
            playbackOverlayTag.classList.add('warning-bg');
            playbackOverlayTag.innerHTML = `<i data-lucide="eye"></i> ${escapeHtml(ev.title)}`;
        } else {
            playbackOverlayTag.classList.add('success-bg');
            playbackOverlayTag.innerHTML = `<i data-lucide="check-circle"></i> ${escapeHtml(ev.title)}`;
        }
        lucide.createIcons();
    }

    // Scrubber progress calculation
    const progressPct = currentEvents.length > 1 ? Math.round((index / (currentEvents.length - 1)) * 100) : 100;
    if (progressBarFill) progressBarFill.style.width = `${progressPct}%`;
    if (progressHandle) progressHandle.style.left = `${progressPct}%`;
    if (currentScrubTime) currentScrubTime.textContent = `${ev.timestamp} (Event ${index + 1}/${currentEvents.length})`;

    // Facebox position animation
    if (playbackFaceBox) {
        if (ev.category === 'GAZE') {
            playbackFaceBox.style.borderColor = 'var(--warning)';
            playbackFaceBox.style.transform = 'translate(-30px, 0)';
        } else if (ev.category === 'DEVICE' || ev.severity === 'HIGH_RISK') {
            playbackFaceBox.style.borderColor = 'var(--danger)';
            playbackFaceBox.style.transform = 'translate(0, 10px)';
        } else {
            playbackFaceBox.style.borderColor = 'var(--success)';
            playbackFaceBox.style.transform = 'translate(0, 0)';
        }
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
    const searchInput = document.getElementById('timelineSearchInput');
    const clearBtn = document.getElementById('clearSearchBtn');
    if (searchInput) {
        searchInput.value = term;
        currentSearchQuery = term;
        if (clearBtn) clearBtn.classList.add('show');
        fetchTimelineData();
        searchInput.focus();
    }
}

// ─── Reset All Filters ───────────────────────────────────────
function resetFilters() {
    currentSearchQuery = '';
    currentCategory = 'ALL';
    currentSeverity = 'ALL';

    const searchInput = document.getElementById('timelineSearchInput');
    const clearBtn = document.getElementById('clearSearchBtn');
    const severitySelect = document.getElementById('severityFilterSelect');

    if (searchInput) searchInput.value = '';
    if (clearBtn) clearBtn.classList.remove('show');
    if (severitySelect) severitySelect.value = 'ALL';

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
    const title = document.getElementById('inspectorTitle');
    const desc = document.getElementById('inspectorDesc');
    if (title) title.textContent = 'No Event Selected';
    if (desc) desc.textContent = 'Adjust search query or category filters above to inspect timeline telemetry.';
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
