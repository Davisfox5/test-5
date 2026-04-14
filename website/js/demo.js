document.addEventListener('DOMContentLoaded', () => {
    // ======== NAVIGATION LOGIC ========
    const navItems = document.querySelectorAll('.nav-item');
    const sections = document.querySelectorAll('.view');
    const viewContainer = document.getElementById('viewContainer');

    function switchView(viewId) {
        sections.forEach(s => s.classList.remove('active'));
        navItems.forEach(n => n.classList.remove('active'));

        const targetSection = document.getElementById(viewId);
        const targetNav = document.querySelector(`.nav-item[data-view="${viewId}"]`);

        if (targetSection) targetSection.classList.add('active');
        if (targetNav) targetNav.classList.add('active');
        
        viewContainer.scrollTop = 0;
    }

    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const viewId = item.getAttribute('data-view');
            if (viewId) switchView(viewId);
        });
    });

    // Handle back links (e.g., from call detail back to overview/calls)
    document.querySelectorAll('.back-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            switchView('overview');
        });
    });

    // Handle clicking a call in the table to view details
    document.querySelectorAll('.clickable-row').forEach(row => {
        row.addEventListener('click', () => {
            switchView('call-detail');
        });
    });

    // ======== MOCK TRANSCRIPT PLAYBACK ========
    const playBtn = document.querySelector('.btn-play');
    const progressBar = document.querySelector('.progress');
    const transcriptEntries = document.querySelectorAll('.transcript-entry');
    const waves = document.querySelectorAll('.wave');
    let isPlaying = false;
    let progress = 35; // Start at a mid-point for visual effect

    if (playBtn) {
        playBtn.addEventListener('click', () => {
            isPlaying = !isPlaying;
            playBtn.innerText = isPlaying ? '⏸' : '▶';
            
            if (isPlaying) {
                simulatePlayback();
            }
        });
    }

    function simulatePlayback() {
        if (!isPlaying) return;
        
        progress += 0.1;
        if (progress > 100) progress = 0;
        
        if (progressBar) progressBar.style.width = `${progress}%`;
        
        // Randomly update "active" waves
        const activeWaveIdx = Math.floor(Math.random() * waves.length);
        waves.forEach((w, i) => {
            w.classList.toggle('active', i === activeWaveIdx);
        });

        // Loop through transcript entries based on time (simulated)
        // This is just for visual "sync" feel in the demo
        if (progress > 40 && progress < 60) {
            transcriptEntries.forEach(e => e.classList.remove('active'));
            transcriptEntries[2].classList.add('active');
            transcriptEntries[2].scrollIntoView({ behavior: 'smooth', block: 'center' });
        }

        requestAnimationFrame(simulatePlayback);
    }

    // ======== MODAL LOGIC ========
    const uploadBtn = document.getElementById('uploadBtn');
    const uploadModal = document.getElementById('uploadModal');
    const closeModals = document.querySelectorAll('.close-modal');

    if (uploadBtn && uploadModal) {
        uploadBtn.addEventListener('click', () => {
            uploadModal.classList.add('active');
        });

        uploadModal.addEventListener('click', (e) => {
            if (e.target === uploadModal) uploadModal.classList.remove('active');
        });
    }

    closeModals.forEach(btn => {
        btn.addEventListener('click', () => {
            uploadModal.classList.remove('active');
        });
    });

    // ======== SEARCH HIGHLIGHTING (Basic Mock) ========
    const searchInput = document.querySelector('.header-search input');
    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            const query = e.target.value.toLowerCase();
            if (query.length > 2) {
                // In a real app, this would filter/search
                console.log('Searching for:', query);
            }
        });
    }

    // ======== DASHBOARD CHARTS (Mock) ========
    // Just adding some random bars to the mini charts
    document.querySelectorAll('.mini-chart').forEach(chart => {
        for (let i = 0; i < 20; i++) {
            const bar = document.createElement('div');
            bar.style.cssText = `
                width: 4px;
                height: ${20 + Math.random() * 80}%;
                background: var(--primary);
                opacity: 0.3;
                border-radius: 2px;
                position: absolute;
                bottom: 0;
                left: ${i * 6}px;
            `;
            chart.appendChild(bar);
        }
    });
});
