// theme.js — Light/dark theme toggle
// =========================================================================
// Theme toggle
// =========================================================================

function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    const btn = document.getElementById('theme-toggle');
    btn.textContent = next === 'dark' ? '☆' : '☾';  // star / moon
    btn.title = next === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
}

function _initTheme() {
    const saved = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    const btn = document.getElementById('theme-toggle');
    if (btn) {
        btn.textContent = saved === 'dark' ? '☆' : '☾';
        btn.title = saved === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    }
}
