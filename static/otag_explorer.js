// otag_explorer.js
// ---------------------------------------------------------------------------
// Otag Explorer tab — Galaxy / Radar / Atlas modes powered by D3 v7.
//
// Public entry points (called from templates/index.html):
//   - initOtagExplorer()      → first-render bootstrap, called when the
//                               Otag Explorer tab is shown
//   - setOtagMode(mode)       → "galaxy" | "radar" | "atlas"
//   - setOtagDepth(n)         → 1 | 2 | 3 (re-fetches Galaxy)
//   - openOtag(name)          → switch to Galaxy mode and load a tag
//   - openCard(scryfallId)    → switch to Radar mode and load a card
// ---------------------------------------------------------------------------

(function () {
    'use strict';

    // ----- State --------------------------------------------------------
    const state = {
        mode: 'galaxy',                 // 'galaxy' | 'radar' | 'atlas'
        depth: 2,
        types: new Set(['hierarchy', 'synonym', 'co_occurs', 'implies']),
        center: null,                   // current Galaxy otag
        cardFocus: null,                // current Radar card (id|null)
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
    };

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

        // First render
        if (state.mode === 'galaxy') renderGalaxy();
    };

    // ----- Mode + Depth controls ----------------------------------------
    window.setOtagMode = function (mode) {
        state.mode = mode;
        document.querySelectorAll('[data-otag-mode]').forEach(b => {
            b.classList.toggle('active', b.dataset.otagMode === mode);
        });
        // Show/hide galaxy controls
        const gc = document.querySelector('.otag-galaxy-controls');
        if (gc) gc.style.display = (mode === 'galaxy') ? '' : 'none';
        if (mode === 'galaxy') renderGalaxy();
        else if (mode === 'atlas') renderAtlas();
        else if (mode === 'radar') renderRadar();
    };

    window.setOtagDepth = function (d) {
        state.depth = d;
        document.querySelectorAll('[data-otag-depth]').forEach(b => {
            b.classList.toggle('active', +b.dataset.otagDepth === d);
        });
        if (state.mode === 'galaxy') renderGalaxy();
    };

    window.openOtag = function (name) {
        state.center = name;
        if (state.mode !== 'galaxy') {
            window.setOtagMode('galaxy');
        } else {
            renderGalaxy();
        }
    };

    window.openCard = function (scryfallId) {
        state.cardFocus = { id: scryfallId };
        if (state.mode !== 'radar') {
            window.setOtagMode('radar');
        } else {
            renderRadar();
        }
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
                    state.cardFocus = { id: it.scryfall_id, name: it.label,
                                        set: it.set, cn: it.cn };
                    window.setOtagMode('radar');
                } else {
                    state.center = it.value;
                    window.setOtagMode('galaxy');
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
    function renderGalaxy() {
        clearGraph();
        if (!state.center) {
            renderGalaxyEmpty();
            return;
        }
        setLoading(true);
        const types = Array.from(state.types).join(',');
        const url = `/api/otags/neighborhood?center=${encodeURIComponent(state.center)}&depth=${state.depth}&types=${encodeURIComponent(types)}&max_nodes=80`;
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
        const links = payload.edges.map(e => ({
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

        nodeSel.append('circle')
            .attr('r', d => nodeRadius(d.card_count))
            .attr('fill', d => d.is_center ? '#fbb144' : '#7da9ff')
            .attr('stroke', d => d.is_center ? '#ffffff' : 'none')
            .attr('stroke-width', d => d.is_center ? 2 : 0)
            .attr('opacity', d => d.is_center ? 1 : (d.depth === 1 ? 0.9 : 0.6));

        nodeSel.append('text')
            .attr('x', d => nodeRadius(d.card_count) + 4)
            .attr('dy', 4)
            .style('font-size', '11px')
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
                // Fade-out then warp
                g.transition().duration(250).style('opacity', 0).on('end', () => {
                    g.style('opacity', 1);
                    state.center = d.id;
                    renderGalaxy();
                });
            }
        });

        // Forces
        const center = nodes.find(n => n.is_center);
        if (state.sim) state.sim.stop();
        state.sim = d3.forceSimulation(nodes)
            .force('link', d3.forceLink(links).id(d => d.id).distance(80))
            .force('charge', d3.forceManyBody().strength(-180))
            .force('collide', d3.forceCollide().radius(d => nodeRadius(d.card_count) + 8))
            .force('center', d3.forceCenter(w / 2, h / 2))
            .force('x', d3.forceX(w / 2).strength(0.05))
            .force('y', d3.forceY(h / 2).strength(0.05))
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

    // ----- Radar mode ---------------------------------------------------
    function renderRadar() {
        clearGraph();
        if (!state.cardFocus) {
            setSidebarHtml('<div class="text-muted small">Search for a card to start.</div>');
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
            .then(payload => drawRadar(payload))
            .catch(err => {
                setLoading(false);
                setSidebarHtml(`<div class="text-danger">${escapeHtml(err.error || 'Error')}</div>`);
            });
    }

    function drawRadar(payload) {
        setLoading(false);
        const { w, h } = dims();
        const cx = w / 2, cy = h / 2;
        const g = root();

        // Card image clip + circle
        const card = payload.card || {};
        const otags = payload.otags || [];

        const defs = svg().select('defs');
        defs.selectAll('#radar-clip').remove();
        defs.append('clipPath').attr('id', 'radar-clip')
            .append('circle').attr('cx', cx).attr('cy', cy).attr('r', 70);

        if (card.image_url) {
            g.append('image')
                .attr('href', card.image_url)
                .attr('x', cx - 70).attr('y', cy - 95)
                .attr('width', 140).attr('height', 190)
                .attr('clip-path', 'url(#radar-clip)');
        }
        g.append('circle').attr('cx', cx).attr('cy', cy).attr('r', 70)
            .attr('fill', 'none').attr('stroke', '#fbb144').attr('stroke-width', 2);

        if (!otags.length) {
            g.append('text').attr('x', cx).attr('y', cy + 130)
                .attr('text-anchor', 'middle')
                .style('font-size', '14px')
                .text('No Tagger data for this printing');
            const slug = (card.set || '') + '/' + (card.cn || '');
            g.append('a').attr('href', `https://tagger.scryfall.com/card/${slug}`)
                .attr('target', '_blank')
              .append('text').attr('x', cx).attr('y', cy + 152)
                .attr('text-anchor', 'middle')
                .style('font-size', '12px').style('fill', '#7da9ff')
                .text('Open in Scryfall Tagger →');
            renderSidebarForCard(payload);
            return;
        }

        // Layout: angularly distribute by stable hash, radius by depth
        const positions = otags.map(o => {
            const depth = (o.ancestors || []).length;
            const r = 90 + depth * 60;
            const angle = (hashStr(o.otag) % 360) * Math.PI / 180;
            return Object.assign({}, o, {
                x: cx + Math.cos(angle) * r,
                y: cy + Math.sin(angle) * r,
                r,
            });
        });

        // Background concentric rings
        const rings = Array.from(new Set(positions.map(p => p.r))).sort((a, b) => a - b);
        g.insert('g', ':first-child').attr('class', 'rings').selectAll('circle')
            .data(rings).join('circle')
            .attr('cx', cx).attr('cy', cy).attr('r', d => d)
            .attr('fill', 'none').attr('stroke', '#2a2d39')
            .attr('stroke-dasharray', '2,3');

        // Edges: connect tags whose ancestor list contains another tag in the set.
        const idx = new Map(positions.map(p => [p.otag, p]));
        const links = [];
        for (const p of positions) {
            for (const a of (p.ancestors || [])) {
                const target = idx.get(a);
                if (target) links.push({ source: p, target });
            }
        }
        g.append('g').selectAll('line').data(links).join('line')
            .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x).attr('y2', d => d.target.y)
            .attr('stroke', '#ffffff').attr('stroke-opacity', 0.4);

        const nodeSel = g.append('g').selectAll('g.radar-node').data(positions)
            .join('g').attr('class', 'radar-node')
            .attr('transform', d => `translate(${d.x},${d.y})`)
            .style('cursor', 'pointer');

        nodeSel.append('circle')
            .attr('r', d => Math.max(6, Math.min(10, nodeRadius(d.card_count))))
            .attr('fill', '#7da9ff').attr('opacity', 0.8);
        nodeSel.append('text')
            .attr('x', 10).attr('dy', 4)
            .style('font-size', '11px').text(d => d.otag);

        nodeSel.on('click', (ev, d) => window.openOtag(d.otag));
        nodeSel.on('mouseenter', (ev, d) => {
            // Build a lightweight payload so the sidebar render works
            renderSidebarForOtag(d.otag, {
                nodes: [{ id: d.otag, card_count: d.card_count }],
                edges: [],
            });
        });

        renderSidebarForCard(payload);
    }

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
