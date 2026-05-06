// app.js — MTG Card Sorter Web UI bootstrap
// Loads last; wires up DOMContentLoaded behaviors that span multiple modules.

document.addEventListener('DOMContentLoaded', () => {
    _initTheme();
    // Load initial data
    loadBinLocations();
    loadBinTable();
    drawMotionCanvas();
    loadPriorityWishlistSources();

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

    document.querySelector('a[href="#tab-database"]')?.addEventListener('shown.bs.tab', () => {
        loadDbInfo();
    });

    document.querySelector('a[href="#tab-bins"]').addEventListener('shown.bs.tab', () => {
        loadBinTable();
        loadBinConfigList();
        loadOverflowConfig();
    });

    document.querySelector('a[href="#tab-calibration"]').addEventListener('shown.bs.tab', () => {
        loadCameraOffset();
        loadSourceBinsStatus();
    });

    // Sort Session tab: start the camera feed immediately so the user
    // can see the staging area before pressing Start Session. Without
    // this, the feed only started on the `session_started` socket
    // event, so opening the tab just showed a blank camera box and
    // made the whole screen look "dead".
    document.querySelector('a[href="#tab-session"]').addEventListener('shown.bs.tab', () => {
        startCameraFeed('session-camera-feed');
    });

    document.querySelector('a[href="#tab-motion"]')?.addEventListener('shown.bs.tab', () => {
        drawMotionCanvas();
    });

    // Initial state poll
    apiGet('/api/status').then(data => {
        currentState = data.state;
        updateStateBadge();
        updateSessionButtons();
        if (data.motion) {
            motionState = data.motion;
            drawMotionCanvas();
        }
    }).catch(() => {});

    // Camera status poll
    setInterval(async () => {
        try {
            const data = await apiGet('/api/camera/status');
            document.getElementById('cam-status').textContent = data.active ? 'Active' : 'Inactive';
            document.getElementById('cam-fps').textContent = data.fps || '--';
        } catch (e) {}
    }, 5000);

    updateStateBadge();

    // Phase 0A: enrichment sources panel + collection sub-nav
    loadEnrichmentSources();
    wireCollectionSubnav();

    // Sort Configuration panel
    loadSortConfigList();

    // Phase 4.23: dashboard bin routing (queries + counts per bin)
    loadDashboardBinRouting();
});
