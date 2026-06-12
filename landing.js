/* ============================================================
   ProctorAI Landing Page — landing.js
   All interactions wired. No dead clicks.
   ============================================================ */

// ─── Particle System (Liquid Glass Background) ───────────────
function initParticles() {
    const canvas = document.getElementById('particles-bg');
    if (!canvas) return;
    const ctx = canvas.getContext('2d', { alpha: false });
    let width, height;
    let particles = [];

    function resize() {
        width = window.innerWidth;
        height = window.innerHeight;
        canvas.width = width;
        canvas.height = height;
    }
    window.addEventListener('resize', resize);
    resize();

    class Particle {
        constructor() {
            this.x = Math.random() * width;
            this.y = Math.random() * height;
            this.vx = (Math.random() - 0.5) * 0.5;
            this.vy = (Math.random() - 0.5) * 0.5;
            this.radius = Math.random() * 40 + 20;
            this.hue = Math.random() > 0.5 ? 210 : 250; // Blue and Purple
        }
        update() {
            this.x += this.vx;
            this.y += this.vy;
            if (this.x < -this.radius) this.x = width + this.radius;
            if (this.x > width + this.radius) this.x = -this.radius;
            if (this.y < -this.radius) this.y = height + this.radius;
            if (this.y > height + this.radius) this.y = -this.radius;
        }
        draw() {
            ctx.beginPath();
            const gradient = ctx.createRadialGradient(this.x, this.y, 0, this.x, this.y, this.radius);
            gradient.addColorStop(0, `hsla(${this.hue}, 100%, 60%, 0.15)`);
            gradient.addColorStop(1, `hsla(${this.hue}, 100%, 60%, 0)`);
            ctx.fillStyle = gradient;
            ctx.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
            ctx.fill();
        }
    }

    // Initialize 50 particles
    for (let i = 0; i < 50; i++) {
        particles.push(new Particle());
    }

    function animate() {
        ctx.fillStyle = '#050505';
        ctx.fillRect(0, 0, width, height);
        particles.forEach(p => {
            p.update();
            p.draw();
        });
        requestAnimationFrame(animate);
    }
    animate();
}

// ─── Toast Notification System ──────────────────────────────
function showToast(message, type = 'success', duration = 3000) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = `
            position: fixed; bottom: 2rem; right: 2rem;
            z-index: 99999; display: flex; flex-direction: column;
            gap: 0.75rem; pointer-events: none;
        `;
        document.body.appendChild(container);
    }

    const icons = { success: '✓', error: '✕', info: 'ℹ', warning: '⚠' };
    const colors = {
        success: 'rgba(50, 215, 75, 0.15)', border: { success: 'rgba(50,215,75,0.4)',
        error: 'rgba(255,69,58,0.4)', info: 'rgba(10,132,255,0.4)', warning: 'rgba(255,214,10,0.4)' },
        error: 'rgba(255,69,58,0.15)', info: 'rgba(10,132,255,0.15)', warning: 'rgba(255,214,10,0.15)'
    };
    const textColors = { success: '#32d74b', error: '#ff453a', info: '#0a84ff', warning: '#ffd60a' };

    const toast = document.createElement('div');
    toast.style.cssText = `
        display: flex; align-items: center; gap: 0.75rem;
        padding: 0.875rem 1.25rem; border-radius: 14px;
        background: rgba(20,20,22,0.95); backdrop-filter: blur(20px);
        border: 1px solid ${colors.border[type] || colors.border.info};
        color: #fff; font-family: 'Inter', sans-serif;
        font-size: 0.9rem; font-weight: 500;
        box-shadow: 0 8px 32px rgba(0,0,0,0.5);
        transform: translateX(120%); transition: transform 0.35s cubic-bezier(0.16,1,0.3,1);
        pointer-events: all; min-width: 240px; max-width: 360px;
        background: rgba(20,20,22,0.95);
    `;
    toast.innerHTML = `
        <span style="font-size:1.1rem;color:${textColors[type]}">${icons[type]}</span>
        <span>${message}</span>
    `;
    container.appendChild(toast);

    requestAnimationFrame(() => {
        requestAnimationFrame(() => { toast.style.transform = 'translateX(0)'; });
    });

    setTimeout(() => {
        toast.style.transform = 'translateX(120%)';
        setTimeout(() => toast.remove(), 400);
    }, duration);
}

// ─── Page Transition Overlay ────────────────────────────────
function navigateWithTransition(url, delay = 900) {
    let overlay = document.getElementById('page-transition');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'page-transition';
        overlay.style.cssText = `
            position: fixed; inset: 0; background: #000;
            z-index: 99998; opacity: 0;
            transition: opacity 0.4s ease;
            display: flex; align-items: center; justify-content: center;
            pointer-events: none;
        `;
        overlay.innerHTML = `
            <div style="display:flex;flex-direction:column;align-items:center;gap:1rem">
                <div style="
                    width:40px;height:40px;border:3px solid rgba(255,255,255,0.1);
                    border-top-color:#0a84ff;border-radius:50%;
                    animation:spin 0.8s linear infinite;
                "></div>
                <span style="color:rgba(255,255,255,0.5);font-family:'Inter',sans-serif;font-size:0.875rem;">Loading...</span>
            </div>
        `;
        const style = document.createElement('style');
        style.textContent = '@keyframes spin{to{transform:rotate(360deg)}}';
        document.head.appendChild(style);
        document.body.appendChild(overlay);
    }
    overlay.style.pointerEvents = 'all';
    requestAnimationFrame(() => { overlay.style.opacity = '1'; });
    setTimeout(() => { window.location.href = url; }, delay);
}

// ─── Mobile Drawer ───────────────────────────────────────────
function initMobileMenu() {
    const mobileBtn = document.querySelector('.mobile-menu-btn');
    if (!mobileBtn) return;

    // Build drawer
    const drawer = document.createElement('div');
    drawer.id = 'mobile-drawer';
    drawer.style.cssText = `
        position: fixed; top: 0; left: 0; right: 0; bottom: 0;
        background: rgba(5,5,5,0.97); backdrop-filter: blur(30px);
        z-index: 9999; display: flex; flex-direction: column;
        align-items: center; justify-content: center; gap: 2rem;
        transform: translateY(-100%); transition: transform 0.4s cubic-bezier(0.16,1,0.3,1);
        padding: 2rem;
    `;
    drawer.innerHTML = `
        <button id="drawer-close" style="
            position:absolute;top:1.5rem;right:1.5rem;
            background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);
            color:#fff;width:44px;height:44px;border-radius:50%;font-size:1.25rem;
            cursor:pointer;display:flex;align-items:center;justify-content:center;
        ">✕</button>
        <a href="#features" class="drawer-link">Features</a>
        <a href="#how-it-works" class="drawer-link">How it Works</a>
        <a href="#showcase" class="drawer-link">Platform</a>
        <button class="drawer-cta" onclick="navigateWithTransition('monitoring.html')">
            Launch Platform →
        </button>
    `;
    document.body.appendChild(drawer);

    // Drawer link styles
    const drawerStyles = document.createElement('style');
    drawerStyles.textContent = `
        .drawer-link {
            font-family: 'Inter', sans-serif; font-size: 1.75rem; font-weight: 600;
            color: rgba(255,255,255,0.8); text-decoration: none;
            transition: color 0.2s; letter-spacing: -0.02em;
        }
        .drawer-link:hover { color: #fff; }
        .drawer-cta {
            margin-top: 1rem; padding: 1rem 2.5rem; border-radius: 99px;
            background: #0a84ff; color: #fff; border: none;
            font-family: 'Inter', sans-serif; font-size: 1.1rem; font-weight: 600;
            cursor: pointer; transition: all 0.3s;
        }
        .drawer-cta:hover { background: #007aff; transform: scale(1.03); }
    `;
    document.head.appendChild(drawerStyles);

    let isOpen = false;
    function openDrawer() {
        isOpen = true;
        drawer.style.transform = 'translateY(0)';
        document.body.style.overflow = 'hidden';
    }
    function closeDrawer() {
        isOpen = false;
        drawer.style.transform = 'translateY(-100%)';
        document.body.style.overflow = '';
    }

    mobileBtn.addEventListener('click', openDrawer);
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);

    // Close on anchor click
    drawer.querySelectorAll('.drawer-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            closeDrawer();
            const targetId = link.getAttribute('href');
            setTimeout(() => {
                const el = document.querySelector(targetId);
                if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }, 400);
        });
    });
}

// ─── Main Init ───────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {

    // Initialize background particles
    initParticles();

    // 1. Scroll reveal (Intersection Observer)
    const revealElements = document.querySelectorAll('.reveal-up');
    const revealObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) entry.target.classList.add('active');
        });
    }, { root: null, threshold: 0.1, rootMargin: '0px 0px -50px 0px' });
    revealElements.forEach(el => revealObserver.observe(el));

    // Stats count up animation
    const animateStat = (el) => {
        if (el.dataset.animated) return;
        el.dataset.animated = "true";
        const text = el.innerText;
        const match = text.match(/([0-9.]+)(.*)/);
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
            el.innerText = (isFloat ? currentVal.toFixed(1) : Math.floor(currentVal)) + suffix;
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
    document.querySelectorAll('.stat-number').forEach(el => statsObserver.observe(el));

    // 2. Navbar scroll effect
    const navbar = document.querySelector('.navbar');
    if (navbar) {
        window.addEventListener('scroll', () => {
            if (window.scrollY > 50) {
                navbar.style.background = 'rgba(5,5,5,0.92)';
                navbar.style.borderColor = 'rgba(255,255,255,0.12)';
                navbar.style.boxShadow = '0 10px 40px rgba(0,0,0,0.6)';
            } else {
                navbar.style.background = 'rgba(255,255,255,0.03)';
                navbar.style.borderColor = 'rgba(255,255,255,0.08)';
                navbar.style.boxShadow = 'none';
            }
        }, { passive: true });
    }

    // 3. Smooth scroll for anchor links (with navbar offset & tab support)
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function(e) {
            e.preventDefault();
            const targetId = this.getAttribute('href');
            if (!targetId || targetId === '#') return;
            
            const el = document.querySelector(targetId);
            if (el) {
                // If the target is a tab, switch it
                if (el.classList.contains('tab-content')) {
                    document.querySelectorAll('.tab-link').forEach(l => {
                        if (l.getAttribute('href') === targetId) l.classList.add('active');
                        else l.classList.remove('active');
                    });
                    
                    document.querySelectorAll('.tab-content').forEach(c => {
                        c.classList.remove('active');
                        c.style.display = 'none';
                    });
                    
                    el.style.display = 'block';
                    // Trigger reflow
                    void el.offsetWidth;
                    el.classList.add('active');
                }

                const navH = navbar ? navbar.offsetHeight + 30 : 80;
                const top = el.getBoundingClientRect().top + window.scrollY - navH;
                window.scrollTo({ top, behavior: 'smooth' });
            }
        });
    });

    // 4. Mobile menu
    initMobileMenu();

    // 5. ── Launch Platform / CTA buttons ──────────────────────
    // All buttons that navigate to monitoring.html
    document.querySelectorAll(
        '#launchPlatformBtn, .btn-launch-platform, [data-nav="monitoring"]'
    ).forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            showToast('Launching Platform…', 'info', 1200);
            navigateWithTransition('monitoring.html', 900);
        });
    });

    // Hero "Start Monitoring" button
    const heroButtons = document.querySelectorAll('.hero-buttons .btn');
    heroButtons.forEach(btn => {
        const href = btn.getAttribute('href');
        if (href && href.includes('monitoring')) {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                showToast('Launching Platform…', 'info', 1200);
                navigateWithTransition('monitoring.html', 900);
            });
        }
        if (href && href.includes('reports')) {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                navigateWithTransition('reports.html', 600);
            });
        }
    });

    // CTA Section "Launch Platform" button
    document.querySelectorAll('.cta-buttons .btn').forEach(btn => {
        const href = btn.getAttribute('href');
        if (href && href.includes('monitoring')) {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                showToast('Launching Platform…', 'info', 1200);
                navigateWithTransition('monitoring.html', 900);
            });
        }
        if (href && href.includes('features') || href === '#features') {
            // already handled by smooth scroll above
        }
    });

    // Showcase cards — navigate with transition and save context
    document.querySelectorAll('.showcase-card').forEach(card => {
        const href = card.getAttribute('href');
        if (href && !href.startsWith('#')) {
            card.addEventListener('click', (e) => {
                e.preventDefault();
                sessionStorage.setItem('navSource', 'unified-suite');
                sessionStorage.setItem('scrollPos', window.scrollY);
                navigateWithTransition(href, 500);
            });
        }
    });

    // Check if returning from a suite page
    const returning = sessionStorage.getItem('returningFromSuite');
    if (returning === 'true' || (window.performance && window.performance.navigation && window.performance.navigation.type === 2)) {
        sessionStorage.removeItem('returningFromSuite');
        const savedPos = sessionStorage.getItem('scrollPos');
        if (savedPos) {
            setTimeout(() => {
                window.scrollTo({ top: parseInt(savedPos, 10), behavior: 'smooth' });
            }, 100);
        } else {
            const el = document.getElementById('unified-suite');
            if (el) setTimeout(() => el.scrollIntoView({ behavior: 'smooth' }), 100);
        }
    }

    // 6. ── Footer links ───────────────────────────────────────
    const footerLinks = {
        'Documentation': () => showToast('Documentation — Coming Soon', 'info'),
        'API Reference': () => showToast('API Reference — Coming Soon', 'info'),
        'Support Center': () => showToast('Support Center — Coming Soon', 'info'),
        'Privacy': () => showToast('Privacy Policy — Coming Soon', 'info'),
        'About Us': () => showToast('About Us — Coming Soon', 'info'),
        'Careers': () => showToast('Careers — Coming Soon', 'info'),
        'Contact Support': () => showToast('Contact Support — Coming Soon', 'info'),
        'Terms of Service': () => showToast('Terms of Service — Coming Soon', 'info'),
        'Security Overview': () => showToast('Security Overview — Coming Soon', 'info'),
        'Privacy Policy': () => showToast('Privacy Policy — Coming Soon', 'info'),
        'Live Monitoring': () => navigateWithTransition('monitoring.html', 500),
        'Identity Enrollment': () => navigateWithTransition('enrollment.html', 500),
    };

    document.querySelectorAll('.footer-links-group a').forEach(link => {
        const text = link.textContent.trim();
        const href = link.getAttribute('href');
        // Only intercept # links or known coming-soon
        if (href === '#' || footerLinks[text]) {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                if (footerLinks[text]) footerLinks[text]();
            });
        }
    });

    // 7. ── Parallax floating cards ────────────────────────────
    const mattersVisual = document.querySelector('.matters-visual');
    if (mattersVisual) {
        mattersVisual.addEventListener('mousemove', (e) => {
            const rect = mattersVisual.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const moveX = (x - rect.width / 2) / 20;
            const moveY = (y - rect.height / 2) / 20;
            const c1 = document.querySelector('.card-1');
            const c2 = document.querySelector('.card-2');
            if (c1) c1.style.transform = `translate(${moveX}px, ${moveY}px)`;
            if (c2) c2.style.transform = `translate(${-moveX}px, ${-moveY}px)`;
        }, { passive: true });
        mattersVisual.addEventListener('mouseleave', () => {
            const c1 = document.querySelector('.card-1');
            const c2 = document.querySelector('.card-2');
            if (c1) c1.style.transform = 'translate(0,0)';
            if (c2) c2.style.transform = 'translate(0,0)';
        });
    }
});
