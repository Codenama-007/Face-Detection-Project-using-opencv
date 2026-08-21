/**
 * ProctorAI — Enterprise Theme Transition Engine
 * Handles Night Ops ↔ Day Ops switching with:
 * - Sliding toggle switch
 * - Rotating / morphing moon & sun icons
 * - Expanding ambient circular ripple originating from the toggle button
 * - Global 600ms cubic-bezier(0.22, 1, 0.36, 1) surface transitions
 * - Instant zero-flash localStorage persistence
 */
(function () {
    // 1. Restore saved theme immediately (default to 'dark')
    const savedTheme = localStorage.getItem('proctorai_theme') || 'dark';
    document.documentElement.setAttribute('data-theme', savedTheme);

    // 2. Global Toggle Handler
    window.toggleTheme = function (event) {
        const current = document.documentElement.getAttribute('data-theme') || 'dark';
        const next = current === 'dark' ? 'light' : 'dark';

        const prefersReducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

        // 3. Create and expand soft ambient ripple from the click coordinates
        if (!prefersReducedMotion && document.body) {
            createThemeRipple(event, next);
        }

        // 4. Trigger global smooth transition on all surfaces
        document.documentElement.classList.add('theme-transitioning');
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('proctorai_theme', next);

        updateThemeToggleUI(next);

        setTimeout(() => {
            document.documentElement.classList.remove('theme-transitioning');
        }, 700);
    };

    // 5. Expand circular wave from the clicked toggle button
    function createThemeRipple(event, nextTheme) {
        try {
            let x = window.innerWidth / 2;
            let y = 40;

            if (event && event.currentTarget) {
                const rect = event.currentTarget.getBoundingClientRect();
                x = rect.left + rect.width / 2;
                y = rect.top + rect.height / 2;
            } else if (event && event.clientX && event.clientY) {
                x = event.clientX;
                y = event.clientY;
            } else {
                const firstBtn = document.querySelector('.theme-toggle-btn');
                if (firstBtn) {
                    const rect = firstBtn.getBoundingClientRect();
                    x = rect.left + rect.width / 2;
                    y = rect.top + rect.height / 2;
                }
            }

            const maxRadius = Math.hypot(
                Math.max(x, window.innerWidth - x),
                Math.max(y, window.innerHeight - y)
            );

            const ripple = document.createElement('div');
            ripple.className = 'theme-ripple-overlay ' + (nextTheme === 'light' ? 'to-light' : 'to-dark');
            ripple.style.left = `${x}px`;
            ripple.style.top = `${y}px`;
            ripple.style.width = `${maxRadius * 2.2}px`;
            ripple.style.height = `${maxRadius * 2.2}px`;
            ripple.style.marginLeft = `-${maxRadius * 1.1}px`;
            ripple.style.marginTop = `-${maxRadius * 1.1}px`;

            document.body.appendChild(ripple);

            // Force reflow and animate expansion
            requestAnimationFrame(() => {
                ripple.classList.add('active');
            });

            setTimeout(() => {
                if (ripple.parentNode) {
                    ripple.parentNode.removeChild(ripple);
                }
            }, 750);
        } catch (e) {
            console.warn('Ripple transition fallback:', e);
        }
    }

    // 6. Build and update the toggle switch DOM
    function renderToggleButtons() {
        const isDark = (document.documentElement.getAttribute('data-theme') || 'dark') === 'dark';

        document.querySelectorAll('.theme-toggle-btn').forEach(btn => {
            btn.setAttribute('aria-label', isDark ? 'Switch to Day Operations (Light Mode)' : 'Switch to Night Operations (Dark Mode)');
            btn.setAttribute('title', isDark ? 'Switch to Day Operations' : 'Switch to Night Operations');

            // Build sliding switch track and thumb if not already structured
            if (!btn.querySelector('.theme-toggle-thumb')) {
                btn.innerHTML = `
                    <div class="theme-track-icons">
                        <span class="track-moon"><i data-lucide="moon" style="width:11px;height:11px;"></i></span>
                        <span class="track-sun"><i data-lucide="sun" style="width:11px;height:11px;"></i></span>
                    </div>
                    <div class="theme-toggle-thumb">
                        <span class="theme-thumb-icon moon"><i data-lucide="moon" style="width:12px;height:12px;"></i></span>
                        <span class="theme-thumb-icon sun"><i data-lucide="sun" style="width:12px;height:12px;"></i></span>
                    </div>
                `;
            }
        });

        if (window.lucide && typeof lucide.createIcons === 'function') {
            lucide.createIcons();
        }
    }

    function updateThemeToggleUI(theme) {
        const isDark = theme === 'dark';
        document.querySelectorAll('.theme-toggle-btn').forEach(btn => {
            btn.setAttribute('aria-label', isDark ? 'Switch to Day Operations (Light Mode)' : 'Switch to Night Operations (Dark Mode)');
            btn.setAttribute('title', isDark ? 'Switch to Day Operations' : 'Switch to Night Operations');
        });
    }

    // 7. Initialize on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', renderToggleButtons);
    } else {
        renderToggleButtons();
    }
})();
