/* ============================================================
   ProctorAI Reports Page — reports.js
   All interactions wired. No dead clicks.
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
        border-radius:14px;background:rgba(12,12,14,0.97);backdrop-filter:blur(20px);
        border:1px solid ${borders[type]||borders.info};color:#fff;
        font-family:'Inter',-apple-system,sans-serif;font-size:0.875rem;font-weight:500;
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

function downloadJSON(data, filename) {
    downloadBlob(JSON.stringify(data, null, 2), filename, 'application/json');
}

function downloadCSV(rows, filename) {
    const csv = rows.map(r => r.join(',')).join('\n');
    downloadBlob(csv, filename, 'text/csv');
}

function downloadPDF(filename) {
    // Minimal valid PDF blob
    const pdfContent = `%PDF-1.4
1 0 obj<</Type /Catalog /Pages 2 0 R>>endobj
2 0 obj<</Type /Pages /Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type /Page /MediaBox[0 0 612 792]/Parent 2 0 R /Resources<<>>>>endobj
xref
0 4
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
trailer<</Size 4/Root 1 0 R>>
startxref
217
%%EOF`;
    downloadBlob(pdfContent, filename, 'application/pdf');
}

// ─── Chart defaults ──────────────────────────────────────────
Chart.defaults.color = '#86868b';
Chart.defaults.font.family = "'Inter', -apple-system, sans-serif";
Chart.defaults.scale.grid.color = 'rgba(255, 255, 255, 0.04)';

const commonOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: { display: false },
        tooltip: {
            backgroundColor: 'rgba(10,10,11,0.9)',
            titleColor: '#fff', bodyColor: '#e5e7eb',
            padding: 16, cornerRadius: 12,
            borderColor: 'rgba(255,255,255,0.1)', borderWidth: 1,
            titleFont: { size: 14, weight: '600' },
            bodyFont: { size: 13 }, boxPadding: 6, usePointStyle: true,
        }
    },
    interaction: { mode: 'index', intersect: false },
    animation: { duration: 700, easing: 'easeOutQuart' }
};

// ─── Dataset library for date ranges ────────────────────────
const chartDatasets = {
    trust: {
        '7':  { labels: ['<50%','50-60%','60-70%','70-80%','80-90%','90-100%'], data: [30, 90, 200, 380, 1100, 1400] },
        '30': { labels: ['<50%','50-60%','60-70%','70-80%','80-90%','90-100%'], data: [120, 350, 800, 1500, 4200, 5480] },
        '90': { labels: ['<50%','50-60%','60-70%','70-80%','80-90%','90-100%'], data: [310, 820, 2100, 4200, 11000, 14200] },
        '365': { labels: ['<50%','50-60%','60-70%','70-80%','80-90%','90-100%'], data: [800, 2200, 6000, 14000, 35000, 44000] },
    },
    risk: {
        '7':  { labels: ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'], data: [22, 35, 18, 62, 30, 15, 28] },
        '30': { labels: ['Day 1','Day 2','Day 3','Day 4','Day 5','Day 6','Day 7'], data: [35,42,38,85,45,25,30] },
        '90': { labels: ['Wk 1','Wk 2','Wk 3','Wk 4','Wk 5','Wk 6','Wk 7','Wk 8','Wk 9','Wk 10','Wk 11','Wk 12'], data: [28,35,42,55,38,70,45,30,80,65,50,38] },
        '365': { labels: ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'], data: [120,150,180,210,190,250,220,170,300,280,260,210] },
    },
    kpi: {
        '7':  { students: '2,890', exams: '12', highRisk: '31', trustScore: '91.8%', suspicious: '892', completed: '1,740' },
        '30': { students: '12,450', exams: '45', highRisk: '142', trustScore: '92.4%', suspicious: '3,892', completed: '8,920' },
        '90': { students: '38,200', exams: '134', highRisk: '421', trustScore: '91.2%', suspicious: '11,240', completed: '27,050' },
        '365': { students: '145,200', exams: '520', highRisk: '1,820', trustScore: '91.5%', suspicious: '45,300', completed: '108,000' },
    }
};

document.addEventListener('DOMContentLoaded', () => {

    // ── 1. Trust Score Chart ──────────────────────────────────
    const ctxTrust = document.getElementById('trustScoreChart').getContext('2d');
    const gradAccent = ctxTrust.createLinearGradient(0, 0, 0, 400);
    gradAccent.addColorStop(0, 'rgba(0,102,204,0.9)');
    gradAccent.addColorStop(1, 'rgba(0,102,204,0.2)');

    const trustChart = new Chart(ctxTrust, {
        type: 'bar',
        data: {
            labels: chartDatasets.trust['30'].labels,
            datasets: [{ label: 'Number of Students', data: chartDatasets.trust['30'].data, backgroundColor: gradAccent, borderRadius: 6, borderSkipped: false, barPercentage: 0.6 }]
        },
        options: { ...commonOptions, scales: { y: { beginAtZero: true, grid: { drawBorder: false }, ticks: { padding: 10 } }, x: { grid: { display: false, drawBorder: false }, ticks: { padding: 10 } } } }
    });

    // ── 2. Risk Trend Chart ──────────────────────────────────
    const ctxRisk = document.getElementById('riskTrendChart').getContext('2d');
    const gradDanger = ctxRisk.createLinearGradient(0, 0, 0, 300);
    gradDanger.addColorStop(0, 'rgba(255,59,48,0.3)');
    gradDanger.addColorStop(1, 'rgba(255,59,48,0.0)');

    const riskChart = new Chart(ctxRisk, {
        type: 'line',
        data: {
            labels: chartDatasets.risk['30'].labels,
            datasets: [{ label: 'High Risk Events', data: chartDatasets.risk['30'].data, borderColor: '#ff3b30', backgroundColor: gradDanger, borderWidth: 2.5, tension: 0.4, fill: true, pointBackgroundColor: '#000', pointBorderColor: '#ff3b30', pointBorderWidth: 2, pointRadius: 4, pointHoverRadius: 6, pointHoverBackgroundColor: '#ff3b30' }]
        },
        options: { ...commonOptions, scales: { y: { beginAtZero: true, grid: { drawBorder: false }, ticks: { padding: 10 } }, x: { grid: { display: false, drawBorder: false }, ticks: { padding: 10 } } } }
    });

    // ── 3. Violation Doughnut Chart ──────────────────────────
    const ctxViolation = document.getElementById('violationChart').getContext('2d');
    new Chart(ctxViolation, {
        type: 'doughnut',
        data: {
            labels: ['Looking Away', 'Multiple Faces', 'Face Missing', 'Audio Flag', 'Other'],
            datasets: [{ data: [42,28,18,12,10], backgroundColor: ['#ffcc00','#ff3b30','#0066cc','#34c759','#86868b'], borderWidth: 0, hoverOffset: 6 }]
        },
        options: { ...commonOptions, cutout: '78%', plugins: { legend: { display: true, position: 'right', labels: { color: '#e5e7eb', usePointStyle: true, padding: 20, font: { size: 13 } } }, tooltip: { ...commonOptions.plugins.tooltip, callbacks: { label: ctx => ` ${ctx.label}: ${ctx.parsed}%` } } } }
    });

    // ── 4. Performance Scatter Chart ─────────────────────────
    const ctxPerf = document.getElementById('performanceChart').getContext('2d');
    const scatterData = Array.from({ length: 150 }, () => {
        let trust = Math.random() > 0.3 ? 80 + Math.random() * 20 : 50 + Math.random() * 50;
        return { x: trust, y: 40 + Math.random() * 60 };
    });
    new Chart(ctxPerf, {
        type: 'scatter',
        data: { datasets: [{ label: 'Student Metrics', data: scatterData, backgroundColor: 'rgba(0,102,204,0.7)', borderColor: 'transparent', pointRadius: 4, pointHoverRadius: 7, pointHoverBackgroundColor: '#0066cc', pointHoverBorderColor: '#fff', pointHoverBorderWidth: 2 }] },
        options: { ...commonOptions, plugins: { tooltip: { ...commonOptions.plugins.tooltip, callbacks: { label: ctx => ` Trust: ${ctx.parsed.x.toFixed(1)}% | Score: ${ctx.parsed.y.toFixed(1)}%` } } }, scales: { x: { title: { display: true, text: 'Integrity Trust Score (%)', color: '#86868b', font: { weight: '500' } }, grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false }, min: 40, max: 100 }, y: { title: { display: true, text: 'Examination Marks (%)', color: '#86868b', font: { weight: '500' } }, grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false }, min: 0, max: 100 } } }
    });

    // ── 5. Date Range Switcher ───────────────────────────────
    const dateRangeBtn = document.querySelector('.date-range');
    if (dateRangeBtn) {
        // Build dropdown
        const dropdown = document.createElement('div');
        dropdown.id = 'date-dropdown';
        dropdown.style.cssText = `
            position:absolute;top:calc(100% + 0.5rem);right:0;
            background:rgba(12,12,14,0.98);backdrop-filter:blur(20px);
            border:1px solid rgba(255,255,255,0.1);border-radius:14px;
            padding:0.5rem;min-width:180px;z-index:9999;
            box-shadow:0 16px 40px rgba(0,0,0,0.5);
            opacity:0;transform:translateY(-8px) scale(0.97);
            transition:all 0.2s cubic-bezier(0.16,1,0.3,1);pointer-events:none;
        `;
        const ranges = [
            { label: 'Last 7 Days', days: '7' },
            { label: 'Last 30 Days', days: '30', active: true },
            { label: 'Last 90 Days', days: '90' },
            { label: 'Last Year', days: '365' },
        ];
        ranges.forEach(r => {
            const item = document.createElement('div');
            item.style.cssText = `
                padding:0.75rem 1rem;border-radius:10px;cursor:pointer;
                font-size:0.875rem;font-weight:500;color:${r.active ? '#fff' : '#86868b'};
                background:${r.active ? 'rgba(255,255,255,0.08)' : 'transparent'};
                transition:background 0.15s,color 0.15s;
                font-family:'Inter',-apple-system,sans-serif;
            `;
            item.textContent = r.label;
            item.dataset.days = r.days;
            item.addEventListener('mouseenter', () => { if (!item.classList.contains('selected')) { item.style.background = 'rgba(255,255,255,0.05)'; item.style.color = '#fff'; } });
            item.addEventListener('mouseleave', () => { if (!item.classList.contains('selected')) { item.style.background = 'transparent'; item.style.color = '#86868b'; } });
            item.addEventListener('click', () => switchDateRange(r.days, r.label, item));
            if (r.active) item.classList.add('selected');
            dropdown.appendChild(item);
        });

        // Wrap dateRangeBtn in a relative container
        dateRangeBtn.style.position = 'relative';
        dateRangeBtn.style.cursor = 'pointer';
        dateRangeBtn.appendChild(dropdown);

        let dropOpen = false;
        dateRangeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            dropOpen = !dropOpen;
            if (dropOpen) {
                dropdown.style.opacity = '1';
                dropdown.style.transform = 'translateY(0) scale(1)';
                dropdown.style.pointerEvents = 'all';
            } else {
                closeDropdown();
            }
        });
        document.addEventListener('click', closeDropdown);
        function closeDropdown() {
            dropOpen = false;
            dropdown.style.opacity = '0';
            dropdown.style.transform = 'translateY(-8px) scale(0.97)';
            dropdown.style.pointerEvents = 'none';
        }

        let currentRange = '30';
        function switchDateRange(days, label, clickedItem) {
            if (days === currentRange) { closeDropdown(); return; }
            currentRange = days;

            // Update button label
            dateRangeBtn.querySelector('span').textContent = label;

            // Update active state in dropdown
            dropdown.querySelectorAll('div').forEach(el => {
                el.classList.remove('selected');
                el.style.background = 'transparent';
                el.style.color = '#86868b';
            });
            clickedItem.classList.add('selected');
            clickedItem.style.background = 'rgba(255,255,255,0.08)';
            clickedItem.style.color = '#fff';

            // Update charts
            const td = chartDatasets.trust[days];
            trustChart.data.labels = td.labels;
            trustChart.data.datasets[0].data = td.data;
            trustChart.update();

            const rd = chartDatasets.risk[days];
            riskChart.data.labels = rd.labels;
            riskChart.data.datasets[0].data = rd.data;
            riskChart.update();

            // Update KPI cards
            const kd = chartDatasets.kpi[days];
            const kpiVals = document.querySelectorAll('.kpi-value');
            if (kpiVals[0]) kpiVals[0].textContent = kd.students;
            if (kpiVals[1]) kpiVals[1].textContent = kd.exams;
            if (kpiVals[2]) kpiVals[2].textContent = kd.highRisk;
            if (kpiVals[3]) kpiVals[3].textContent = kd.trustScore;
            if (kpiVals[4]) kpiVals[4].textContent = kd.suspicious;
            if (kpiVals[5]) kpiVals[5].textContent = kd.completed;

            closeDropdown();
            showToast(`Showing analytics for ${label}`, 'info', 2000);
        }
    }

    // ── 6. Sample analytics data ─────────────────────────────
    const analyticsData = {
        generated: new Date().toISOString(),
        period: 'Last 30 Days',
        kpi: { totalStudents: 12450, activeExams: 45, highRiskStudents: 142, avgTrustScore: '92.4%', suspiciousEvents: 3892, examsCompleted: 8920 },
        violations: [
            { type: 'Looking Away', count: 1634, percentage: 42 },
            { type: 'Multiple Faces', count: 1090, percentage: 28 },
            { type: 'Face Missing', count: 700, percentage: 18 },
            { type: 'Audio Flag', count: 467, percentage: 12 },
        ],
        trustDistribution: [
            { range: '<50%', students: 120 }, { range: '50-60%', students: 350 },
            { range: '60-70%', students: 800 }, { range: '70-80%', students: 1500 },
            { range: '80-90%', students: 4200 }, { range: '90-100%', students: 5480 },
        ]
    };

    // ── 7. Export buttons ────────────────────────────────────
    function withLoadingToast(label, fn, btnElement) {
        if (btnElement) btnElement.classList.add('loading');
        showToast(`${label} starting…`, 'info', 1500);
        setTimeout(() => {
            fn();
            if (btnElement) btnElement.classList.remove('loading');
            showToast(`${label} complete!`, 'success');
        }, 1200);
    }

    // Export Report (top header button)
    const exportBtnTop = document.getElementById('exportBtnTop');
    if (exportBtnTop) {
        exportBtnTop.addEventListener('click', () => {
            withLoadingToast('Export Report', () => {
                downloadJSON(analyticsData, 'proctorAI-report.json');
            }, exportBtnTop);
        });
    }

    // Export as PDF
    document.querySelectorAll('.btn-outline').forEach(btn => {
        if (btn.textContent.includes('PDF')) {
            btn.addEventListener('click', () => {
                withLoadingToast('PDF Export', () => downloadPDF('proctorAI-analytics.pdf'), btn);
            });
        }
        if (btn.textContent.includes('CSV')) {
            btn.addEventListener('click', () => {
                withLoadingToast('CSV Export', () => {
                    const csvRows = [
                        ['Type', 'Count', 'Percentage'],
                        ...analyticsData.violations.map(v => [v.type, v.count, v.percentage + '%'])
                    ];
                    downloadCSV(csvRows, 'proctorAI-violations.csv');
                }, btn);
            });
        }
    });

    // Download Full Analytics Data
    document.querySelectorAll('.btn-primary').forEach(btn => {
        if (btn.textContent.includes('Full Analytics') || btn.textContent.includes('Download Full')) {
            btn.addEventListener('click', () => {
                withLoadingToast('Analytics Download', () => {
                    downloadJSON(analyticsData, 'proctorAI-analytics.json');
                }, btn);
            });
        }
    });

    // ── 8. Footer links ──────────────────────────────────────
    document.querySelectorAll('.footer-links a, .glass-footer .footer-links a').forEach(link => {
        const href = link.getAttribute('href');
        if (href === '#' || href === '' || !href) {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                showToast(`${link.textContent.trim()} — Coming Soon`, 'info');
            });
        }
    });

    // ── 9. More Horizontal icon btn (chart section) ──────────
    document.querySelectorAll('.icon-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            showToast('Chart options — Coming Soon', 'info', 2000);
        });
    });

    // ── 10. System Status click ──────────────────────────────
    const footerMeta = document.querySelector('.footer-meta');
    if (footerMeta) {
        footerMeta.style.cursor = 'pointer';
        footerMeta.addEventListener('click', () => {
            showToast('All systems operational ✓', 'success');
        });
    }

    // ── 11. KPI card hover effect ────────────────────────────
    document.querySelectorAll('.kpi-card').forEach(card => {
        card.style.cursor = 'default';
        card.title = 'Click for details';
        card.addEventListener('click', () => {
            const val = card.querySelector('.kpi-value')?.textContent;
            const label = card.querySelector('.kpi-header span')?.textContent;
            if (label && val) showToast(`${label}: ${val}`, 'info', 2500);
        });
    });

    // ── 12. Stats Count up animation ─────────────────────────
    const animateStat = (el) => {
        if (el.dataset.animated) return;
        el.dataset.animated = "true";
        const text = el.innerText;
        const hasComma = text.includes(',');
        const cleanText = text.replace(/,/g, '');
        const match = cleanText.match(/([0-9.]+)(.*)/);
        if (!match) return;
        const endVal = parseFloat(match[1]);
        const suffix = match[2];
        const duration = 2000;
        const isFloat = match[1].includes('.');
        let startTimestamp = null;
        
        const step = (timestamp) => {
            if (!startTimestamp) startTimestamp = timestamp;
            const progress = Math.min((timestamp - startTimestamp) / duration, 1);
            const easeOut = progress === 1 ? 1 : 1 - Math.pow(2, -10 * progress);
            const currentVal = endVal * easeOut;
            let valStr = isFloat ? currentVal.toFixed(1) : Math.floor(currentVal).toString();
            if (hasComma) {
                valStr = parseInt(valStr, 10).toLocaleString('en-US');
            }
            el.innerText = valStr + suffix;
            if (progress < 1) {
                window.requestAnimationFrame(step);
            } else {
                el.innerText = text;
            }
        };
        window.requestAnimationFrame(step);
    };

    const statsObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                animateStat(entry.target);
                statsObserver.unobserve(entry.target);
            }
        });
    }, { threshold: 0.5 });
    document.querySelectorAll('.kpi-value').forEach(el => statsObserver.observe(el));

    // ── 13. Back Button Logic ────────────────────────────────
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
