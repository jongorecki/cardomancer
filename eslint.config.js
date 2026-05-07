// ESLint flat config for the static/ frontend.
//
// The frontend is currently a set of <script> tags loaded into a Jinja
// template — no bundler, no module system. Functions are global by
// design so inline onclick="foo()" handlers in templates/*.html resolve.
// Phase 0 modularization preserves this contract: modules live in
// static/modules/ but explicitly attach their public API to window.
//
// As we migrate to ES modules + addEventListener (Phase 5), tighten
// this config (no-undef on globals, prefer-const, etc.).

export default [
    {
        files: ['static/**/*.js'],
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: 'script',
            globals: {
                // Browser
                window: 'readonly',
                document: 'readonly',
                console: 'readonly',
                fetch: 'readonly',
                alert: 'readonly',
                confirm: 'readonly',
                prompt: 'readonly',
                FormData: 'readonly',
                URLSearchParams: 'readonly',
                URL: 'readonly',
                Image: 'readonly',
                Blob: 'readonly',
                FileReader: 'readonly',
                requestAnimationFrame: 'readonly',
                cancelAnimationFrame: 'readonly',
                setTimeout: 'readonly',
                clearTimeout: 'readonly',
                setInterval: 'readonly',
                clearInterval: 'readonly',
                location: 'readonly',
                history: 'readonly',
                navigator: 'readonly',
                localStorage: 'readonly',
                sessionStorage: 'readonly',
                Event: 'readonly',
                CustomEvent: 'readonly',
                MutationObserver: 'readonly',

                // Third-party libraries loaded via <script>
                io: 'readonly',           // socket.io client
                bootstrap: 'readonly',    // Bootstrap 5 JS

                // Project globals attached to window by feature modules.
                // Listed permissively for now; tighten as modules formalize.
                socket: 'writable',
                addLog: 'readonly',
                apiGet: 'readonly',
                apiPost: 'readonly',
                showApiError: 'readonly',
                escapeHtml: 'readonly',
            },
        },
        rules: {
            'no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
            'no-undef': 'warn',
            'no-console': 'off',
            'eqeqeq': ['warn', 'smart'],
            'no-var': 'warn',
            'prefer-const': 'warn',
        },
    },
    {
        ignores: [
            'static/otag_explorer.js',         // legacy — slated for removal in Phase 1
            'node_modules/**',
            '.claude/**',
            'backup/**',
            'archive/**',
        ],
    },
];
