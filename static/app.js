// app.js — MTG Card Sorter Web UI bootstrap
// Loads last; wires up DOMContentLoaded behaviors that span multiple modules.

document.addEventListener('DOMContentLoaded', () => {
    _initTheme();
    // Load initial data
    loadBinLocations();
    loadBinTable();
    drawMotionCanvas();
    loadPriorityWishlistSources();

    // Home tab is the default landing tab — populate its status strip
    // immediately so the operator doesn't see placeholder dashes on
    // first paint. Refresh every time the Home tab is re-shown so the
    // "last session" line is accurate after finishing a sort.
    if (typeof loadHomeStatus === 'function') {
        loadHomeStatus();
    }
    document.querySelector('a[href="#tab-home"]')?.addEventListener(
        'shown.bs.tab', () => {
            if (typeof loadHomeStatus === 'function') {
                loadHomeStatus();
            }
        }
    );

    // Load collection data when tab is shown
    document.querySelector('a[href="#tab-collection"]').addEventListener('shown.bs.tab', () => {
        loadCollectionStats();
        loadInventory();
        loadSessionHistory();
        loadBoxList();
        loadBoxSummary();
        loadWishlist();
        loadReviewQueue();
    });

    // Sortable column headers
    document.querySelectorAll('#tab-collection th.sortable').forEach(th => {
        th.style.cursor = 'pointer';
        th.addEventListener('click', () => sortInventory(th.dataset.sort));
    });

    // Add Card autocomplete
    _initAddCardAutocomplete();

    // Database stats live inside the Settings modal under the "Card data
    // (Scryfall)" card. Refresh them whenever the operator opens Settings
    // (rather than on every page load — the call hits disk + counts
    // images, which is non-trivial). Also refresh whenever a db update
    // job finishes; that hookup lives in modules/socket.js.
    document.getElementById('settings-modal')?.addEventListener('shown.bs.modal', () => {
        loadDbInfo();
        // The other source of remote-data refresh status lives in the
        // same panel; reload that too so the operator sees fresh state.
        if (typeof loadEnrichmentSources === 'function') {
            loadEnrichmentSources();
        }
    });

    // Sort tab: refresh bin data and start the staging camera feed so the
    // user can see the platform before pressing Start Session. Bin Setup
    // and Sort Session both fold into the Sort tab post-Phase-1b, so all
    // these loaders fire on the same shown.bs.tab event.
    const sortTab = document.querySelector('a[href="#tab-sort"]');
    if (sortTab) {
        sortTab.addEventListener('shown.bs.tab', () => {
            loadBinTable();
            loadBinConfigList();
            loadOverflowConfig();
            startCameraFeed('session-camera-feed');
            // Paint the Source-bin gauge from cached state (if any)
            // so the user sees the most recent number on tab open
            // without waiting for the next probe.
            if (typeof _refreshStackEstimateFromApi === 'function') {
                _refreshStackEstimateFromApi();
            }
        });
    }

    // Setup tab: load camera offset + source-bin status when the user
    // navigates to the tab (Calibration content lives here post-Phase-1b).
    const setupTab = document.querySelector('a[href="#tab-setup"]');
    if (setupTab) {
        setupTab.addEventListener('shown.bs.tab', () => {
            loadCameraOffset();
            loadSourceBinsStatus();
        });
    }

    // Initial state poll
    apiGet('/api/status').then(data => {
        currentState = data.state;
        updateStateBadge();
        updateSessionButtons();
        if (typeof initSortStageFromState === 'function') {
            initSortStageFromState(data.state);
        }
        if (data.motion) {
            motionState = data.motion;
            drawMotionCanvas();
        }
    }).catch(() => {});

    updateStateBadge();

    // Phase 0A: enrichment sources panel + collection sub-nav
    loadEnrichmentSources();
    wireCollectionSubnav();

    // Sort Configuration panel
    loadSortConfigList();

    // Phase 4.23: dashboard bin routing (queries + counts per bin)
    loadDashboardBinRouting();
});
