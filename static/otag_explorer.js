// otag_explorer.js
// ---------------------------------------------------------------------------
// Otag Explorer tab — Galaxy / Outline / Tree / Atlas modes powered by D3 v7.
//
// Modes:
//   Galaxy   — force-directed neighborhood graph (most general).
//   Outline  — focused indented list: parent chain above, focus row, then
//              siblings (other children of immediate parent) and children.
//              Closest analog to the "browse a tag's place in the
//              taxonomy" mental model.
//   Tree     — top-down hierarchy view of every otag on a given card.
//   Atlas    — Louvain cluster overview.
//
// Public entry points (called from templates/index.html):
//   - initOtagExplorer()      → first-render bootstrap, called when the
//                               Otag Explorer tab is shown
//   - setOtagMode(mode)       → "galaxy" | "outline" | "tree" | "atlas"
//   - setOtagDepth(n)         → 1 | 2 | 3 (re-fetches Galaxy)
//   - openOtag(name)          → switch to Galaxy mode and load a tag
//                               (outline mode preserved if currently in it)
//   - openCard(scryfallId)    → switch to Tree mode and load a card
//   - otagGoBack()            → pop one entry off the navigation stack
//   - otagReset()             → clear focus + history, return to empty state
// ---------------------------------------------------------------------------

(function () {
    'use strict';

    // ----- State --------------------------------------------------------
    const state = {
        mode: 'galaxy',                 // 'galaxy' | 'outline' | 'tree' | 'atlas'
        depth: 1,                       // default: 1 hop (was 2 — too dense)
        types: new Set(['hierarchy', 'synonym', 'co_occurs', 'implies']),
        center: null,                   // current Galaxy / Outline otag
        cardFocus: null,                // current Tree card (id|null)
        sim: null,                      // active d3.forceSimulation
        zoom: null,
        initialized: false,
        searchAbort: null,
        // Reused across redraws for hover edge highlighting
        nodeSel: null,
        edgeSel: null,
        labelSel: null,
        // Cluster cache for cross-mode lookups
        clusters: null,
        // Navigation: each warp pushes a snapshot {mode, center, cardFocus}
        // onto history; otagGoBack() pops one off and restores it.
        history: [],
    };

    function snapshotState() {
        return { mode: state.mode, center: state.center,
                 cardFocus: state.cardFocus ? Object.assign({}, state.cardFocus) : null };
    }
    function restoreSnapshot(s) {
        state.mode = s.mode;
        state.center = s.center;
        state.cardFocus = s.cardFocus;
        // Re-render without pushing another history entry
        applyModeUI();
        if (state.mode === 'galaxy') renderGalaxy();
        else if (state.mode === 'outline') renderOutline();
        else if (state.mode === 'tree') renderTree();
        else if (state.mode === 'atlas') renderAtlas();
    }
    function pushHistory() {
        // Don't push duplicates of the same snapshot
        const cur = snapshotState();
        const last = state.history[state.history.length - 1];
        if (last && last.mode === cur.mode && last.center === cur.center
            && JSON.stringify(last.cardFocus) === JSON.stringify(cur.cardFocus)) {
            return;
        }
        state.history.push(cur);
        // Cap depth so the stack doesn't grow unbounded over a long session
        if (state.history.length > 64) state.history.shift();
        updateNavButtons();
    }
    function updateNavButtons() {
        const back = document.getElementById('btn-otag-back');
        if (back) back.disabled = state.history.length === 0;
    }

    // ----- Utilities ----------------------------------------------------
    function svg() { return d3.select('#otag-explorer-svg'); }
    function root() { return svg().select('#otag-zoom-root'); }
    function setLoading(on) {
        const el = document.getElementById('otag-explorer-loading');
        if (el) el.style.display = on ? 'block' : 'none';
    }
    function clearGraph() {
        root().selectAll('*').remove();
        if (state.sim) { state.sim.stop(); state.sim = null; }
    }
    function fetchJson(url) {
        return fetch(url).then(r => {
            if (!r.ok) {
                return r.json().then(j => Promise.reject(j))
                    .catch(() => Promise.reject({ error: 'HTTP ' + r.status }));
            }
            return r.json();
        });
    }
    function dims() {
        const c = document.getElementById('otag-explorer-canvas');
        if (!c) return { w: 800, h: 600 };
        return { w: c.clientWidth || 800, h: c.clientHeight || 600 };
    }

    // Stable hash used for deterministic Radar layout.
    function hashStr(s) {
        let h = 2166136261;
        for (let i = 0; i < s.length; i++) {
            h ^= s.charCodeAt(i);
            h = Math.imul(h, 16777619);
        }
        return (h >>> 0);
    }

    function nodeRadius(card_count) {
        const cc = Math.max(0, +card_count || 0);
        return 4 + 4 * Math.sqrt(Math.log2(cc + 1));
    }

    // ----- Initial bootstrap --------------------------------------------
    window.initOtagExplorer = function () {
        if (state.initialized) {
            // Re-fit zoom in case window resized while tab was hidden
            return;
        }
        state.initialized = true;

        // Wire up zoom
        const z = d3.zoom()
            .scaleExtent([0.1, 8])
            .on('zoom', (event) => {
                root().attr('transform', event.transform);
            });
        svg().call(z);
        state.zoom = z;

        // Wire search input
        const input = document.getElementById('otag-search-input');
        if (input) {
            let debounceTimer = null;
            input.addEventListener('input', () => {
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => doSearch(input.value), 200);
            });
            input.addEventListener('blur', () => {
                // Hide dropdown after a short delay so click handler runs
                setTimeout(hideSearchDropdown, 150);
            });
            input.addEventListener('focus', () => {
                if (input.value) doSearch(input.value);
            });
        }

        // Wire type checkboxes
        document.querySelectorAll('.otag-type-toggle').forEach(cb => {
            cb.addEventListener('change', () => {
                const t = cb.dataset.otagType;
                if (cb.checked) state.types.add(t);
                else state.types.delete(t);
                if (state.mode === 'galaxy') renderGalaxy();
            });
        });

        // Initial nav button state (no history yet → Back disabled)
        updateNavButtons();

        // First render
        if (state.mode === 'galaxy') renderGalaxy();
    };

    // ----- Mode + Depth controls ----------------------------------------
    function applyModeUI() {
        document.querySelectorAll('[data-otag-mode]').forEach(b => {
            b.classList.toggle('active', b.dataset.otagMode === state.mode);
        });
        const gc = document.querySelector('.otag-galaxy-controls');
        if (gc) gc.style.display = (state.mode === 'galaxy') ? '' : 'none';
        // Outline mode renders into a separate HTML container; SVG canvas
        // is hidden when outline is active and shown otherwise.
        const svgEl = document.getElementById('otag-explorer-svg');
        const outEl = document.getElementById('otag-outline-container');
        if (svgEl && outEl) {
            const isOutline = (state.mode === 'outline');
            svgEl.style.display = isOutline ? 'none' : 'block';
            outEl.style.display = isOutline ? '' : 'none';
        }
    }

    window.setOtagMode = function (mode) {
        state.mode = mode;
        applyModeUI();
        if (mode === 'galaxy') renderGalaxy();
        else if (mode === 'outline') renderOutline();
        else if (mode === 'atlas') renderAtlas();
        else if (mode === 'tree') renderTree();
    };

    window.setOtagDepth = function (d) {
        state.depth = d;
        document.querySelectorAll('[data-otag-depth]').forEach(b => {
            b.classList.toggle('active', +b.dataset.otagDepth === d);
        });
        if (state.mode === 'galaxy') renderGalaxy();
    };

    window.openOtag = function (name) {
        // Push the current view onto the nav stack before warping
        pushHistory();
        state.center = name;
        // Preserve outline mode when navigating between otags so the
        // user can drill the tree without bouncing to Galaxy. Tree and
        // Atlas always switch to Galaxy on a tag click — those modes
        // don't support tag-as-center.
        if (state.mode === 'outline') {
            renderOutline();
        } else if (state.mode === 'galaxy') {
            renderGalaxy();
        } else {
            window.setOtagMode('galaxy');
        }
    };

    window.openCard = function (scryfallId, meta) {
        pushHistory();
        state.cardFocus = Object.assign({ id: scryfallId }, meta || {});
        if (state.mode !== 'tree') {
            window.setOtagMode('tree');
        } else {
            renderTree();
        }
    };

    window.otagGoBack = function () {
        const prev = state.history.pop();
        updateNavButtons();
        if (!prev) return;
        restoreSnapshot(prev);
    };

    window.otagReset = function () {
        state.history = [];
        state.center = null;
        state.cardFocus = null;
        updateNavButtons();
        // Stay in the current mode; just clear its focus
        if (state.mode === 'galaxy') renderGalaxy();
        else if (state.mode === 'outline') renderOutline();
        else if (state.mode === 'tree') renderTree();
        else renderAtlas();
    };

    // ----- Search -------------------------------------------------------
    function doSearch(q) {
        q = (q || '').trim();
        if (q.length < 2) { hideSearchDropdown(); return; }
        if (state.searchAbort) state.searchAbort.abort();
        state.searchAbort = new AbortController();
        fetch('/api/otags/search?q=' + encodeURIComponent(q) + '&limit=20',
              { signal: state.searchAbort.signal })
            .then(r => r.json())
            .then(showSearchDropdown)
            .catch(() => { /* ignore aborts */ });
    }
    function hideSearchDropdown() {
        const d = document.getElementById('otag-search-dropdown');
        if (d) { d.style.display = 'none'; d.innerHTML = ''; }
    }
    function showSearchDropdown(items) {
        const d = document.getElementById('otag-search-dropdown');
        if (!d) return;
        if (!items || !items.length) { hideSearchDropdown(); return; }
        d.innerHTML = '';
        items.forEach(it => {
            const row = document.createElement('div');
            row.className = 'otag-search-row';
            const badge = document.createElement('span');
            badge.className = 'badge ' + (it.kind === 'card' ? 'badge-card' : 'badge-otag');
            badge.textContent = it.kind;
            const label = document.createElement('span');
            label.textContent = it.label;
            row.appendChild(badge);
            row.appendChild(label);
            if (it.kind === 'card' && it.set) {
                const meta = document.createElement('span');
                meta.className = 'small text-muted ms-auto';
                meta.textContent = (it.set || '').toUpperCase() + ' ' + (it.cn || '');
                row.appendChild(meta);
            } else if (it.kind === 'otag' && it.card_count != null) {
                const meta = document.createElement('span');
                meta.className = 'small text-muted ms-auto';
                meta.textContent = it.card_count + ' cards';
                row.appendChild(meta);
            }
            row.addEventListener('mousedown', (e) => {
                e.preventDefault(); // keep input from blurring before click
                if (it.kind === 'card') {
                    window.openCard(it.scryfall_id, {
                        name: it.label, set: it.set, cn: it.cn,
                    });
                } else {
                    window.openOtag(it.value);
                }
                hideSearchDropdown();
                const inp = document.getElementById('otag-search-input');
                if (inp) inp.value = '';
            });
            d.appendChild(row);
        });
        d.style.display = 'block';
    }

    // ----- Sidebar ------------------------------------------------------
    function setSidebarHtml(html) {
        const empty = document.getElementById('otag-side-empty');
        const cont  = document.getElementById('otag-side-content');
        if (empty) empty.style.display = html ? 'none' : '';
        if (cont)  cont.innerHTML = html || '';
    }

    function renderSidebarForOtag(tag, payload) {
        // payload: full neighborhood result (we extract the relevant slice)
        const node = (payload?.nodes || []).find(n => n.id === tag) || { card_count: 0 };
        const inEdges = (payload?.edges || []).filter(e => e.dst === tag || e.src === tag);
        const parents = []; const children = []; const synonyms = [];
        const coOccurs = [];
        for (const e of inEdges) {
            const other = e.src === tag ? e.dst : e.src;
            if (e.type === 'hierarchy') {
                if (e.src === tag) parents.push(other);
                else children.push(other);
            } else if (e.type === 'synonym') {
                synonyms.push(other);
            } else if (e.type === 'co_occurs') {
                coOccurs.push({ tag: other, w: e.weight });
            }
        }
        coOccurs.sort((a, b) => b.w - a.w);

        const linkify = (s) => `<span class="otag-side-link" onclick="openOtag(${JSON.stringify(s).replace(/"/g, '&quot;')})">${escapeHtml(s)}</span>`;
        const list = (arr, cap) => {
            if (!arr.length) return '<span class="text-muted small">none</span>';
            const shown = arr.slice(0, cap || arr.length);
            const html = shown.map(linkify).join(', ');
            return arr.length > shown.length ? html + ` <span class="text-muted small">+${arr.length - shown.length} more</span>` : html;
        };

        let html = '';
        html += `<h5 class="mb-1">${escapeHtml(tag)}</h5>`;
        html += `<div class="mb-2"><span class="badge bg-secondary">${node.card_count} cards</span></div>`;
        html += `<div class="otag-side-section"><h6>Parents</h6>${list(parents, 8)}</div>`;
        html += `<div class="otag-side-section"><h6>Children</h6>${list(children, 8)}</div>`;
        html += `<div class="otag-side-section"><h6>Synonyms</h6>${list(synonyms, 8)}</div>`;
        const co5 = coOccurs.slice(0, 5).map(x =>
            `${linkify(x.tag)} <span class="text-muted small">(${x.w.toFixed(2)})</span>`
        ).join(', ') || '<span class="text-muted small">none</span>';
        html += `<div class="otag-side-section"><h6>Top co-occurs</h6>${co5}</div>`;
        html += `<button class="btn btn-sm btn-outline-primary w-100"
                         onclick="alert('Coming soon: jump to Collection filtered by otag:${escapeHtml(tag)}')">
                   Find cards with this otag &rarr;
                 </button>`;
        setSidebarHtml(html);
    }

    function renderSidebarForCard(payload) {
        const c = payload.card || {};
        const otags = payload.otags || [];
        let html = '';
        html += `<h5 class="mb-1">${escapeHtml(c.name || '')}</h5>`;
        html += `<div class="small text-muted mb-2">${escapeHtml((c.set || '').toUpperCase())} #${escapeHtml(c.cn || '')}</div>`;
        if (c.image_url) {
            html += `<img src="${c.image_url}" alt="card" style="max-width:100%; border-radius:8px; margin-bottom:8px"/>`;
        }
        html += `<div class="mb-2"><span class="badge bg-secondary">${otags.length} otags</span></div>`;
        if (otags.length) {
            html += '<div class="otag-side-section"><h6>Tags</h6>';
            html += otags.map(o =>
                `<div class="small mb-1"><span class="otag-side-link" onclick="openOtag(${JSON.stringify(o.otag).replace(/"/g, '&quot;')})">${escapeHtml(o.otag)}</span> <span class="text-muted">(${o.card_count})</span></div>`
            ).join('');
            html += '</div>';
        }
        setSidebarHtml(html);
    }

    function escapeHtml(s) {
        if (s == null) return '';
        return String(s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    // ----- Galaxy mode --------------------------------------------------
    // Default neighborhood density: depth=1, max_nodes=25. The earlier
    // depth=2 / max_nodes=80 produced unreadable hairballs at this scale
    // (e.g. depth 2 from `removal` returned 200+ nodes). Users can opt
    // back into wider views via the depth slider.
    const GALAXY_MAX_NODES = 25;
    // Co_occurs edges with weight below this threshold are visually noise
    // — they connect tags that share <15% of cards. Hidden by default to
    // keep the layout legible; the relations are still in otag_relations
    // for queries.
    const GALAXY_MIN_COOCCURS_WEIGHT = 0.15;

    function renderGalaxy() {
        clearGraph();
        if (!state.center) {
            renderGalaxyEmpty();
            return;
        }
        setLoading(true);
        const types = Array.from(state.types).join(',');
        const url = `/api/otags/neighborhood?center=${encodeURIComponent(state.center)}&depth=${state.depth}&types=${encodeURIComponent(types)}&max_nodes=${GALAXY_MAX_NODES}`;
        fetchJson(url)
            .then(payload => drawGalaxy(payload))
            .catch(err => {
                setLoading(false);
                setSidebarHtml(`<div class="text-danger">Error: ${escapeHtml(err.error || JSON.stringify(err))}</div>`);
            });
    }

    function renderGalaxyEmpty() {
        // Show 30 highest-card-count tags as drifting nodes (no edges).
        setLoading(true);
        // Use clusters endpoint — it conveniently exposes the biggest tags
        // via cluster top_tags.
        fetchJson('/api/otags/clusters?max_clusters=10')
            .then(payload => {
                const seen = new Set();
                const tags = [];
                for (const cl of (payload.clusters || [])) {
                    for (const t of (cl.top_tags || [])) {
                        if (!seen.has(t)) { seen.add(t); tags.push(t); }
                        if (tags.length >= 30) break;
                    }
                    if (tags.length >= 30) break;
                }
                drawDriftingTags(tags);
            })
            .catch(() => setLoading(false));
    }

    function drawDriftingTags(tags) {
        setLoading(false);
        const { w, h } = dims();
        const nodes = tags.map((t, i) => ({
            id: t, label: t, card_count: 100,
            x: w * 0.5 + Math.cos(i / tags.length * 2 * Math.PI) * w * 0.3,
            y: h * 0.5 + Math.sin(i / tags.length * 2 * Math.PI) * h * 0.3,
            depth: 0,
        }));
        const g = root();
        const sel = g.selectAll('g.otag-node').data(nodes, d => d.id)
            .join('g').attr('class', 'otag-node')
            .attr('transform', d => `translate(${d.x},${d.y})`);
        sel.append('circle').attr('r', 8)
            .attr('fill', '#7da9ff').attr('opacity', 0.7);
        sel.append('text').attr('x', 12).attr('dy', 4)
            .style('font-size', '11px').text(d => d.label);
        sel.style('cursor', 'pointer').on('click', (ev, d) => window.openOtag(d.id));

        if (state.sim) state.sim.stop();
        state.sim = d3.forceSimulation(nodes)
            .force('charge', d3.forceManyBody().strength(-30))
            .force('center', d3.forceCenter(w / 2, h / 2))
            .force('collide', d3.forceCollide().radius(60))
            .alphaDecay(0.005)
            .on('tick', () => {
                sel.attr('transform', d => `translate(${d.x},${d.y})`);
            });
    }

    function drawGalaxy(payload) {
        setLoading(false);
        clearGraph();
        const { w, h } = dims();

        // d3 mutates node objects, so make a copy
        const nodes = payload.nodes.map(n => Object.assign({}, n));
        // Filter out weak co_occurs edges and any edges referencing a node
        // we won't render (max_nodes pruning may have dropped some).
        const visibleNodeIds = new Set(nodes.map(n => n.id));
        const links = payload.edges
            .filter(e => visibleNodeIds.has(e.src) && visibleNodeIds.has(e.dst))
            .filter(e => e.type !== 'co_occurs'
                || (e.weight || 0) >= GALAXY_MIN_COOCCURS_WEIGHT)
            .map(e => ({
                source: e.src, target: e.dst, type: e.type, weight: e.weight,
            }));

        const g = root();

        // Edge styling helpers
        function edgeStroke(t) {
            return ({ hierarchy: '#ffffff', synonym: '#5fdcff',
                      co_occurs: '#ff9d4a', implies: '#c79bff',
                      related: '#888', sibling_disjoint: '#555' }[t]) || '#888';
        }
        function edgeOpacity(d) {
            switch (d.type) {
                case 'hierarchy': return 0.7;
                case 'synonym': return 0.85;
                case 'co_occurs': return 0.2 + 0.6 * (d.weight || 0);
                case 'implies': return 0.6;
                default: return 0.15;
            }
        }
        function edgeDash(t) {
            return (t === 'synonym' || t === 'implies') ? '4,3' : null;
        }

        const edgeSel = g.append('g').attr('class', 'otag-edges')
            .selectAll('line').data(links).join('line')
            .attr('stroke', d => edgeStroke(d.type))
            .attr('stroke-opacity', edgeOpacity)
            .attr('stroke-dasharray', d => edgeDash(d.type))
            .attr('stroke-width', 1.5);

        const nodeSel = g.append('g').attr('class', 'otag-nodes')
            .selectAll('g.otag-node').data(nodes, d => d.id).join('g')
            .attr('class', 'otag-node')
            .style('cursor', 'pointer');

        // Beefier center node so the focus is visually anchored. The
        // center radius scales with card_count like neighbors, but with
        // a generous floor so even small-card-count centers (rare) stand
        // out from their satellites.
        function nodeR(d) {
            const base = nodeRadius(d.card_count);
            return d.is_center ? Math.max(14, base + 6) : base;
        }
        nodeSel.append('circle')
            .attr('r', nodeR)
            .attr('fill', d => d.is_center ? '#fbb144' : '#7da9ff')
            .attr('stroke', d => d.is_center ? '#ffffff' : 'none')
            .attr('stroke-width', d => d.is_center ? 2.5 : 0)
            .attr('opacity', d => d.is_center ? 1 : (d.depth === 1 ? 0.85 : 0.55));

        nodeSel.append('text')
            .attr('x', d => nodeR(d) + 4)
            .attr('dy', 4)
            .style('font-size', d => d.is_center ? '13px' : '11px')
            .style('font-weight', d => d.is_center ? '600' : '400')
            .style('fill', d => d.is_center ? '#ffffff' : '#e8e8ee')
            .text(d => d.label);

        // Hover: highlight edges, populate sidebar
        nodeSel.on('mouseenter', function (ev, d) {
            d3.select(this).select('circle').attr('stroke', '#fbb144').attr('stroke-width', 2);
            edgeSel.attr('stroke-opacity', e =>
                (e.source.id === d.id || e.target.id === d.id) ? 1.0 : edgeOpacity(e) * 0.3
            );
            renderSidebarForOtag(d.id, payload);
        });
        nodeSel.on('mouseleave', function (ev, d) {
            d3.select(this).select('circle')
                .attr('stroke', d.is_center ? '#ffffff' : 'none')
                .attr('stroke-width', d.is_center ? 2 : 0);
            edgeSel.attr('stroke-opacity', edgeOpacity);
        });
        nodeSel.on('click', (ev, d) => {
            if (!d.is_center) {
                // Fade-out then warp. window.openOtag pushes history so
                // the Back button can reverse the navigation.
                g.transition().duration(250).style('opacity', 0).on('end', () => {
                    g.style('opacity', 1);
                    window.openOtag(d.id);
                });
            }
        });

        // Forces — tuned for legibility at depth=1, max_nodes=25.
        //   - Link distance 110 (was 80) so labels have room.
        //   - Collide radius wraps the label baseline: nodeR + ~70px for
        //     the label width, breaking up label overlap without
        //     d3-labeler.
        //   - Stronger center node anchor via fx/fy below.
        const center = nodes.find(n => n.is_center);
        if (state.sim) state.sim.stop();
        state.sim = d3.forceSimulation(nodes)
            .force('link', d3.forceLink(links).id(d => d.id).distance(110).strength(0.6))
            .force('charge', d3.forceManyBody().strength(-260))
            .force('collide', d3.forceCollide()
                .radius(d => nodeR(d) + 28).strength(0.85))
            .force('center', d3.forceCenter(w / 2, h / 2))
            .force('x', d3.forceX(w / 2).strength(0.04))
            .force('y', d3.forceY(h / 2).strength(0.04))
            .alphaDecay(0.03)
            .on('tick', () => {
                edgeSel
                    .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
                    .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
                nodeSel.attr('transform', d => `translate(${d.x},${d.y})`);
            });
        if (center) { center.fx = w / 2; center.fy = h / 2; }

        // Auto-stop after ~300 ticks
        let tickCount = 0;
        state.sim.on('tick.cap', () => {
            tickCount++;
            if (tickCount >= 300) state.sim.stop();
        });

        // Initial sidebar: show the center
        renderSidebarForOtag(state.center, payload);
    }

    // ----- Tree mode (replaces former concentric Radar) ----------------
    //
    // Top-down hierarchy view of every otag applied to a card. For each
    // root tag the card belongs to, we render a tree subgraph using
    // d3.tree(), with the card thumbnail in the upper-left.
    //
    // Why tree-not-radial: the data is purely hierarchical (each otag has
    // an ancestor chain), and a top-down tree reads vastly more cleanly
    // than concentric rings — links are unambiguous, labels don't overlap,
    // and parent/child relationships are visually obvious.

    function renderTree() {
        clearGraph();
        if (!state.cardFocus) {
            setSidebarHtml('<div class="text-muted small">Search for a card to start.</div>');
            // Splash text on canvas
            const { w, h } = dims();
            const g = root();
            g.append('text').attr('x', w / 2).attr('y', h / 2)
                .attr('text-anchor', 'middle')
                .style('font-size', '14px').style('fill', '#7da9ff')
                .text('Search for a card to see its otag tree.');
            return;
        }
        setLoading(true);
        const cf = state.cardFocus;
        let url = '/api/otags/by-card?';
        if (cf.id) url += 'id=' + encodeURIComponent(cf.id);
        else if (cf.name) {
            url += 'name=' + encodeURIComponent(cf.name);
            if (cf.set) url += '&set=' + encodeURIComponent(cf.set);
            if (cf.cn) url += '&cn=' + encodeURIComponent(cf.cn);
        } else { setLoading(false); return; }
        fetchJson(url)
            .then(payload => drawTree(payload))
            .catch(err => {
                setLoading(false);
                setSidebarHtml(`<div class="text-danger">${escapeHtml(err.error || 'Error')}</div>`);
            });
    }

    // Build d3.hierarchy roots from the by-card payload.
    //
    // Each otag has an `ancestors` chain (closest parent first, root last).
    // We invert each chain and assemble into a forest of trees keyed by
    // the deepest ancestor (root). Otags with empty ancestors are roots
    // themselves; a card may have multiple roots.
    function buildTreeRoots(otags) {
        // Map each otag to a node object
        const nodeByName = new Map();
        for (const o of otags) {
            nodeByName.set(o.otag, {
                name: o.otag,
                card_count: o.card_count || 0,
                children: [],
                applied: true,    // is this otag actually on the card?
            });
        }
        // Build chain from root → leaf for each otag and stitch into nodes
        const allRoots = new Set();
        for (const o of otags) {
            const chain = [...(o.ancestors || [])].reverse();
            chain.push(o.otag);
            // Ensure intermediate ancestors have nodes too — they may not
            // be on the card directly, but they're in the lineage.
            for (let i = 0; i < chain.length; i++) {
                if (!nodeByName.has(chain[i])) {
                    nodeByName.set(chain[i], {
                        name: chain[i], card_count: 0,
                        children: [], applied: false,
                    });
                }
            }
            // Stitch parent → child
            for (let i = 0; i < chain.length - 1; i++) {
                const parent = nodeByName.get(chain[i]);
                const child = nodeByName.get(chain[i + 1]);
                if (!parent.children.includes(child)) parent.children.push(child);
            }
            allRoots.add(chain[0]);
        }
        // Roots = nodes that nothing else has as a child
        const childSet = new Set();
        for (const node of nodeByName.values()) {
            for (const c of node.children) childSet.add(c.name);
        }
        const roots = [];
        for (const name of allRoots) {
            if (!childSet.has(name)) roots.push(nodeByName.get(name));
        }
        // If a tag had no ancestors AND nothing else points at it, also a root
        for (const o of otags) {
            if (!(o.ancestors && o.ancestors.length) && !childSet.has(o.otag)) {
                const n = nodeByName.get(o.otag);
                if (!roots.includes(n)) roots.push(n);
            }
        }
        return roots;
    }

    function drawTree(payload) {
        setLoading(false);
        const card = payload.card || {};
        const otags = payload.otags || [];
        const { w, h } = dims();
        const g = root();

        // Card thumbnail block in the upper-left
        const THUMB_W = 100, THUMB_H = 140;
        const cardBlock = g.append('g').attr('class', 'tree-card-block')
            .attr('transform', `translate(20, 20)`);
        if (card.image_url) {
            cardBlock.append('image')
                .attr('href', card.image_url)
                .attr('x', 0).attr('y', 0)
                .attr('width', THUMB_W).attr('height', THUMB_H)
                .attr('rx', 6);
        } else {
            cardBlock.append('rect')
                .attr('width', THUMB_W).attr('height', THUMB_H)
                .attr('fill', '#1e2330').attr('rx', 6);
        }
        cardBlock.append('text')
            .attr('x', THUMB_W + 12).attr('y', 18)
            .style('font-size', '14px').style('fill', '#ffffff').style('font-weight', 600)
            .text(card.name || '');
        cardBlock.append('text')
            .attr('x', THUMB_W + 12).attr('y', 36)
            .style('font-size', '11px').style('fill', '#7da9ff')
            .text(`${(card.set || '').toUpperCase()} #${card.cn || ''}`);
        cardBlock.append('text')
            .attr('x', THUMB_W + 12).attr('y', 54)
            .style('font-size', '11px').style('fill', '#9aa0ad')
            .text(`${otags.length} otag${otags.length === 1 ? '' : 's'} applied`);

        if (!otags.length) {
            g.append('text').attr('x', w / 2).attr('y', h / 2)
                .attr('text-anchor', 'middle')
                .style('font-size', '14px').style('fill', '#e8e8ee')
                .text('No Tagger data for this printing.');
            const slug = (card.set || '') + '/' + (card.cn || '');
            g.append('a').attr('href', `https://tagger.scryfall.com/card/${slug}`)
                .attr('target', '_blank')
              .append('text').attr('x', w / 2).attr('y', h / 2 + 24)
                .attr('text-anchor', 'middle')
                .style('font-size', '12px').style('fill', '#7da9ff')
                .text('Open in Scryfall Tagger →');
            renderSidebarForCard(payload);
            return;
        }

        // Build forest from ancestor chains
        const rootsData = buildTreeRoots(otags);

        // Top of the tree starts below the card block
        const TREE_TOP_Y = THUMB_H + 60;
        const TREE_LEFT = 20;
        const TREE_RIGHT_PAD = 40;
        const NODE_DY = 28;       // vertical separation between tree levels
        const NODE_DX_MIN = 110;  // horizontal separation between siblings
        const treeWidth = Math.max(300, w - TREE_LEFT - TREE_RIGHT_PAD);

        // Lay out each root tree side-by-side
        let xCursor = TREE_LEFT;
        const allRendered = [];
        for (const rd of rootsData) {
            const root = d3.hierarchy(rd);
            const depth = (function maxDepth(n) {
                if (!n.children || !n.children.length) return 0;
                return 1 + Math.max(...n.children.map(maxDepth));
            })(root);

            // Width for this subtree: enough room for its leaves
            const leafCount = root.leaves().length;
            const subWidth = Math.max(NODE_DX_MIN, leafCount * NODE_DX_MIN);
            const subHeight = (depth + 1) * (NODE_DY + 18);

            const layout = d3.tree().size([subWidth, subHeight]);
            layout(root);

            const subG = g.append('g').attr('class', 'tree-subgraph')
                .attr('transform', `translate(${xCursor}, ${TREE_TOP_Y})`);

            // Links
            subG.append('g').attr('class', 'tree-links').selectAll('path')
                .data(root.links()).join('path')
                .attr('d', d3.linkVertical()
                    .x(d => d.x)
                    .y(d => d.y))
                .attr('fill', 'none')
                .attr('stroke', '#7da9ff').attr('stroke-opacity', 0.5)
                .attr('stroke-width', 1.5);

            // Nodes
            const nodeSel = subG.append('g').attr('class', 'tree-nodes')
                .selectAll('g.tree-node').data(root.descendants()).join('g')
                .attr('class', 'tree-node')
                .attr('transform', d => `translate(${d.x},${d.y})`)
                .style('cursor', 'pointer');

            nodeSel.append('circle')
                .attr('r', d => d.data.applied ? 6 : 4)
                .attr('fill', d => d.data.applied ? '#fbb144' : '#7da9ff')
                .attr('stroke', '#0c0d10').attr('stroke-width', 1.5)
                .attr('opacity', d => d.data.applied ? 1.0 : 0.7);

            nodeSel.append('text')
                .attr('x', 0).attr('y', -10)
                .attr('text-anchor', 'middle')
                .style('font-size', '11px')
                .style('fill', d => d.data.applied ? '#ffffff' : '#9aa0ad')
                .text(d => d.data.name);

            // Card-count subtitle
            nodeSel.append('text')
                .attr('x', 0).attr('y', 18)
                .attr('text-anchor', 'middle')
                .style('font-size', '9px').style('fill', '#6e7384')
                .text(d => d.data.card_count
                    ? d.data.card_count.toLocaleString() : '');

            nodeSel.on('mouseenter', function (ev, d) {
                d3.select(this).select('circle')
                    .attr('stroke', '#fbb144').attr('stroke-width', 2);
                renderSidebarForOtag(d.data.name, {
                    nodes: [{ id: d.data.name, card_count: d.data.card_count }],
                    edges: [],
                });
            });
            nodeSel.on('mouseleave', function () {
                d3.select(this).select('circle')
                    .attr('stroke', '#0c0d10').attr('stroke-width', 1.5);
            });
            nodeSel.on('click', (ev, d) => window.openOtag(d.data.name));

            allRendered.push({ subG, subWidth });
            xCursor += subWidth + 40;
        }

        // If forest is wider than the canvas, rely on d3.zoom (already wired)
        // for panning; otherwise center it horizontally.
        const totalWidth = xCursor - TREE_LEFT;
        if (totalWidth < w - TREE_LEFT - TREE_RIGHT_PAD) {
            const offset = (w - TREE_LEFT - TREE_RIGHT_PAD - totalWidth) / 2;
            allRendered.forEach((r, i) => {
                const cur = r.subG.attr('transform');
                // re-apply translate with offset
                const m = /translate\(([^,]+),\s*([^)]+)\)/.exec(cur);
                if (m) {
                    r.subG.attr('transform',
                        `translate(${parseFloat(m[1]) + offset}, ${m[2]})`);
                }
            });
        }

        renderSidebarForCard(payload);
    }

    // ----- Outline mode -------------------------------------------------
    //
    // A focused indented browser. For a tag X, render:
    //
    //   ▸ Parent chain (X's parents, grandparents, …) — collapsed by default
    //   ▸ X (highlighted)
    //   ▸ Siblings (other children of X's immediate parent)
    //   ▸ Children of X
    //
    // Click any tag name to warp focus there. Each row shows tag name +
    // card_count. Closest analog to "browse the tag taxonomy" — easy to
    // see why a tag belongs where it does in the hierarchy.

    const OUTLINE_MAX_SIBLINGS = 30;
    const OUTLINE_MAX_CHILDREN = 50;

    function renderOutline() {
        const container = document.getElementById('otag-outline-container');
        if (!container) return;
        if (!state.center) {
            container.innerHTML =
                '<div class="text-muted small">' +
                'Search for a tag (e.g. <code>removal</code>, <code>ramp</code>) ' +
                'or click a node from another mode to start browsing the taxonomy.' +
                '</div>';
            setSidebarHtml('<div class="text-muted small">' +
                'Pick a tag to see its outline.</div>');
            return;
        }

        setLoading(true);
        const url = '/api/otags/neighborhood'
            + '?center=' + encodeURIComponent(state.center)
            + '&depth=2&types=hierarchy&max_nodes=300';
        // Fetch the neighborhood first so we know which tags are visible,
        // THEN fetch examples for just those tags. Two round-trips but
        // each one is fast and the second only fires when the first
        // succeeds.
        fetchJson(url)
            .then(payload => {
                const visibleTags = (payload.nodes || []).map(n => n.id);
                if (!visibleTags.length) {
                    drawOutline(payload, {});
                    return;
                }
                const exUrl = '/api/otags/examples?n=3&otags=' +
                    encodeURIComponent(visibleTags.join(','));
                fetchJson(exUrl)
                    .then(examples => drawOutline(payload, examples))
                    .catch(() => drawOutline(payload, {}));
            })
            .catch(err => {
                setLoading(false);
                container.innerHTML =
                    '<div class="text-danger">Error: ' +
                    escapeHtml(err.error || JSON.stringify(err)) +
                    '</div>';
            });
    }

    function drawOutline(payload, examplesByTag) {
        setLoading(false);
        const center = state.center;
        const container = document.getElementById('otag-outline-container');
        if (!container) return;
        const exMap = examplesByTag || {};

        const nodes = payload.nodes || [];
        const edges = payload.edges || [];

        // Build adjacency:
        //   parents[X]  = array of parent otag names (X is child of these)
        //   children[X] = array of child otag names
        // hierarchy edges store src=child, dst=parent.
        const parents = {};
        const children = {};
        const cardCount = {};
        for (const n of nodes) {
            cardCount[n.id] = n.card_count || 0;
            if (!parents[n.id]) parents[n.id] = [];
            if (!children[n.id]) children[n.id] = [];
        }
        for (const e of edges) {
            if (e.type !== 'hierarchy') continue;
            (parents[e.src] = parents[e.src] || []).push(e.dst);
            (children[e.dst] = children[e.dst] || []).push(e.src);
        }

        // The focus node may have multiple parents (DAG). Use the first
        // (lowest card_count → most-specific) for the sibling listing.
        const focusParents = (parents[center] || [])
            .slice()
            .sort((a, b) => (cardCount[a] || 0) - (cardCount[b] || 0));
        const focusChildren = (children[center] || [])
            .slice()
            .sort((a, b) => (cardCount[b] || 0) - (cardCount[a] || 0));

        const primaryParent = focusParents[0] || null;
        const siblings = primaryParent
            ? (children[primaryParent] || [])
                  .filter(s => s !== center)
                  .sort((a, b) => (cardCount[b] || 0) - (cardCount[a] || 0))
            : [];

        // ---- Render ---------------------------------------------------
        const linkSafe = (n) =>
            JSON.stringify(n).replace(/"/g, '&quot;');

        // Build a quick lookup of descriptions from the neighborhood
        // payload — populated by the Tagger description scrape; NULL for
        // tags that haven't been scraped yet, in which case the row falls
        // back to its "e.g. CardA, CardB" example line.
        const descByTag = {};
        for (const n of nodes) {
            descByTag[n.id] = n.description || null;
        }

        function row(name, opts) {
            opts = opts || {};
            const cc = cardCount[name] || 0;
            const cls = ['otag-outline-row'];
            if (opts.focus) cls.push('otag-outline-focus');
            if (opts.dim) cls.push('otag-outline-dim');
            const indent = (opts.indent || 0) * 24;
            // Description (preferred) or example-cards fallback. The
            // description comes from tag_catalog.description, populated
            // by web_enrichment.scrape_tagger_descriptions. Until a scrape
            // has been run for that tag we show example cards instead so
            // the row is always informative.
            const desc = descByTag[name];
            const exs = (exMap[name] || []);
            let detailHtml = '';
            if (desc) {
                detailHtml = (
                    '<div class="otag-outline-description" ' +
                    'style="padding-left:' + (indent + 22) + 'px;">' +
                    escapeHtml(desc) +
                    '</div>'
                );
            } else if (exs.length) {
                detailHtml = (
                    '<div class="otag-outline-examples" ' +
                    'style="padding-left:' + (indent + 22) + 'px;">' +
                    '<span class="otag-outline-eg-label">e.g.</span> ' +
                    exs.map(e => escapeHtml(e.name)).join(', ') +
                    '</div>'
                );
            }
            return (
                '<div class="' + cls.join(' ') + '" ' +
                'style="padding-left:' + indent + 'px;">' +
                '<span class="otag-outline-marker">' +
                    (opts.marker || '•') + '</span>' +
                '<span class="otag-outline-name" ' +
                    'onclick="openOtag(' + linkSafe(name) + ')" ' +
                    'onmouseenter="otagOutlineHover(' + linkSafe(name) + ')">' +
                    escapeHtml(name) +
                '</span>' +
                '<span class="otag-outline-count">' +
                    cc.toLocaleString() + ' card' + (cc === 1 ? '' : 's') +
                '</span>' +
                '</div>' + detailHtml
            );
        }

        const parts = [];

        // Header
        parts.push(
            '<div class="otag-outline-header">' +
            '<h5 class="mb-1">' + escapeHtml(center) + '</h5>' +
            '<div class="small text-muted">Outline view — click any tag to warp focus.</div>' +
            '</div>'
        );

        // Parents block (top of the list, indented from each other)
        if (focusParents.length) {
            parts.push('<div class="otag-outline-section">' +
                '<h6>Parents</h6>');
            // For each parent, render it indented based on how many
            // ancestors above the focus it sits. We don't have ancestors
            // beyond depth 1 here (only one hop up), so just render them
            // as a flat list with ↑ markers.
            for (const p of focusParents) {
                parts.push(row(p, { marker: '↑', indent: 0 }));
            }
            parts.push('</div>');
        } else {
            parts.push('<div class="otag-outline-section">' +
                '<h6>Parents</h6>' +
                '<div class="text-muted small">' + escapeHtml(center) +
                ' has no parents — it\'s a root tag in the hierarchy.</div>' +
                '</div>');
        }

        // Focus row
        parts.push('<div class="otag-outline-section">' +
            '<h6>Focus</h6>' +
            row(center, { focus: true, marker: '★' }) +
            '</div>');

        // Siblings
        if (primaryParent) {
            const sibLabel = 'Siblings (other children of <code>' +
                escapeHtml(primaryParent) + '</code>)';
            parts.push('<div class="otag-outline-section">' +
                '<h6>' + sibLabel + '</h6>');
            if (!siblings.length) {
                parts.push('<div class="text-muted small">No siblings — ' +
                    escapeHtml(center) + ' is the only child of <code>' +
                    escapeHtml(primaryParent) + '</code>.</div>');
            } else {
                const shown = siblings.slice(0, OUTLINE_MAX_SIBLINGS);
                for (const s of shown) {
                    parts.push(row(s, { dim: true }));
                }
                if (siblings.length > shown.length) {
                    parts.push(
                        '<div class="text-muted small ms-3">+ ' +
                        (siblings.length - shown.length) +
                        ' more sibling tags</div>'
                    );
                }
            }
            parts.push('</div>');
        }

        // Children
        parts.push('<div class="otag-outline-section">' +
            '<h6>Children</h6>');
        if (!focusChildren.length) {
            parts.push('<div class="text-muted small">' +
                escapeHtml(center) +
                ' has no children — it\'s a leaf in the hierarchy.</div>');
        } else {
            const shown = focusChildren.slice(0, OUTLINE_MAX_CHILDREN);
            for (const c of shown) {
                parts.push(row(c, { indent: 1, marker: '↳' }));
            }
            if (focusChildren.length > shown.length) {
                parts.push(
                    '<div class="text-muted small ms-3">+ ' +
                    (focusChildren.length - shown.length) +
                    ' more children</div>'
                );
            }
        }
        parts.push('</div>');

        container.innerHTML = parts.join('');

        // Pre-fill sidebar with focus details
        renderSidebarForOtag(center, payload);
    }

    // Lightweight hover handler for outline rows — populates the sidebar
    // with the hovered tag's details from the current neighborhood payload.
    // Falls back to a single-fetch if the tag isn't in the cache.
    window.otagOutlineHover = function (name) {
        // The current outline render already has the payload available
        // via the closure of drawOutline; for sidebar updates on hover
        // we just rebuild from a tiny synthetic payload using cardCount.
        // Refresh from the live API when possible for richer sidebar
        // (parents, synonyms, top co-occurs of the hovered tag).
        renderSidebarForOtag(name, { nodes: [{ id: name, card_count: 0 }],
                                     edges: [] });
    };

    // ----- Atlas mode ---------------------------------------------------
    function renderAtlas() {
        clearGraph();
        setLoading(true);
        fetchJson('/api/otags/clusters?max_clusters=20')
            .then(payload => {
                state.clusters = payload;
                drawAtlas(payload);
            })
            .catch(err => {
                setLoading(false);
                setSidebarHtml(`<div class="text-danger">${escapeHtml(err.error || 'Error')}</div>`);
            });
    }

    function drawAtlas(payload) {
        setLoading(false);
        const { w, h } = dims();
        const g = root();

        if (payload.warning || !(payload.clusters || []).length) {
            g.append('text').attr('x', w / 2).attr('y', h / 2)
                .attr('text-anchor', 'middle').style('font-size', '14px')
                .text(payload.warning || 'No clusters available');
            g.append('text').attr('x', w / 2).attr('y', h / 2 + 24)
                .attr('text-anchor', 'middle').style('font-size', '12px')
                .style('fill', '#7da9ff')
                .text('Run `python -m web_enrichment.compute_otag_clusters` first.');
            return;
        }

        const clusters = payload.clusters.map(c => Object.assign({}, c));
        const totalSize = clusters.reduce((s, c) => s + c.size, 0);
        // Bubble radius scaled to ~12% of canvas
        const minDim = Math.min(w, h);
        clusters.forEach(c => {
            c.r = Math.max(40, Math.sqrt(c.size / totalSize) * minDim * 0.7);
            c.x = w / 2 + (Math.random() - 0.5) * w * 0.4;
            c.y = h / 2 + (Math.random() - 0.5) * h * 0.4;
        });

        const bubbleSel = g.append('g').selectAll('g.atlas-bubble')
            .data(clusters).join('g').attr('class', 'atlas-bubble');

        bubbleSel.append('circle')
            .attr('r', d => d.r)
            .attr('fill', '#7da9ff').attr('fill-opacity', 0.15)
            .attr('stroke', '#ffffff').attr('stroke-opacity', 0.4)
            .attr('stroke-width', 1);

        bubbleSel.append('text')
            .attr('y', d => -d.r - 6).attr('text-anchor', 'middle')
            .style('font-size', '13px').style('fill', '#fbb144')
            .text(d => d.label + ' (' + d.size + ')');

        // Inner tag labels: place them via per-bubble radial layout
        bubbleSel.each(function (cluster) {
            const inner = d3.select(this);
            const tags = (cluster.top_tags || []).slice(0, 10).map((t, i, arr) => {
                const angle = (i / arr.length) * 2 * Math.PI;
                const inR = cluster.r * 0.65;
                return { tag: t, x: Math.cos(angle) * inR, y: Math.sin(angle) * inR };
            });
            inner.selectAll('text.atlas-tag').data(tags).enter()
                .append('text')
                .attr('class', 'atlas-tag')
                .attr('x', d => d.x).attr('y', d => d.y)
                .attr('text-anchor', 'middle')
                .style('font-size', '10px')
                .style('cursor', 'pointer')
                .style('pointer-events', 'all')
                .text(d => d.tag)
                .on('click', (ev, d) => window.openOtag(d.tag))
                .on('mouseenter', function () {
                    d3.select(this).style('fill', '#fbb144');
                })
                .on('mouseleave', function () {
                    d3.select(this).style('fill', '#e8e8ee');
                });
        });

        if (state.sim) state.sim.stop();
        state.sim = d3.forceSimulation(clusters)
            .force('charge', d3.forceManyBody().strength(-200))
            .force('collide', d3.forceCollide().radius(d => d.r + 8).strength(0.9))
            .force('center', d3.forceCenter(w / 2, h / 2))
            .alphaDecay(0.04)
            .on('tick', () => {
                bubbleSel.attr('transform', d => `translate(${d.x},${d.y})`);
            });

        setSidebarHtml('<div class="text-muted small">Hover or click a tag inside a cluster to explore. Cluster sizes reflect membership in the Louvain community detection over the co-occurrence graph.</div>');
    }
})();
