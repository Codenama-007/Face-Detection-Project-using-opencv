/**
 * ProctorAI — Enterprise Theme Manager
 * Fast, non-flashing theme toggling with localStorage persistence.
 */
(function () {
    // 1. Read stored preference or default to 'dark'
    const savedTheme = localStorage.getItem('proctorai_theme') || 'dark';
    document.documentElement.setAttribute('data-theme', savedTheme);

    // 2. Global Toggle Handler
    window.toggleTheme = function () {
        const current = document.documentElement.getAttribute('data-theme') || 'dark';
        const next = current === 'dark' ? 'light' : 'dark';

        document.documentElement.classList.add('theme-transition');
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('proctorai_theme', next);

        updateThemeToggleIcons(next);

        setTimeout(() => {
            document.documentElement.classList.remove('theme-transition');
        }, 250);
    };

    // 3. Icon Synchronizer
    function updateThemeToggleIcons(theme) {
        const isDark = theme === 'dark';
        document.querySelectorAll('.theme-toggle-btn').forEach(btn => {
            btn.setAttribute('aria-label', isDark ? 'Switch to light mode' : 'Switch to dark mode');
            btn.setAttribute('title', isDark ? 'Switch to light mode' : 'Switch to dark mode');
            const iconSpan = btn.querySelector('[data-theme-icon]');
            if (iconSpan) {
                iconSpan.innerHTML = isDark
                    ? '<i data-lucide="sun" style="width:15px;height:15px;"></i>'
                    : '<i data-lucide="moon" style="width:15px;height:15px;"></i>';
            }
        });
        if (window.lucide && typeof lucide.createIcons === 'function') {
            lucide.createIcons();
        }
    }

    // 4. Bind on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            updateThemeToggleIcons(document.documentElement.getAttribute('data-theme') || 'dark');
        });
    } else {
        updateThemeToggleIcons(savedTheme);
    }
})();
