/**
 * Global Frontend Interactions for KiliLedger Pro (Django Templates + HTMX + Chart.js)
 */
function toggleMobileSidebar() {
    const sidebar = document.getElementById('app-sidebar');
    if (sidebar) {
        sidebar.classList.toggle('-translate-x-full');
    }
}

// Close slide-over drawer on Escape key
document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
        const drawer = document.getElementById('drawer-container');
        if (drawer && drawer.innerHTML.trim() !== '') {
            drawer.innerHTML = '';
        }
    }
});
