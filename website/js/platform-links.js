/*
 * Platform link rewriter.
 *
 * Marketing + demo pages use canonical '/app/*' hrefs for the SPA
 * (e.g. /app/sign-in, /app/signup). When the SPA is hosted on a
 * different origin (subdomain or a separate deploy) set
 * window.LINDA_APP_URL_BASE on the page (or via a tiny inline script)
 * and this rewriter swaps the prefix at load time so we don't need to
 * fork the markup per environment.
 */
(function () {
    'use strict';
    function base() {
        try {
            return (window.LINDA_APP_URL_BASE || '').replace(/\/+$/, '');
        } catch (e) { return ''; }
    }
    // Expose a helper so dynamically-rendered widgets (demo gate, etc.)
    // can build platform URLs without re-implementing the override
    // logic. ``path`` should start with '/app/' or '/' (e.g. '/sign-in').
    window.lindaAppUrl = function lindaAppUrl(path) {
        var p = String(path || '/');
        if (p.indexOf('/app/') === 0) p = p.slice(4); // drop the leading '/app'
        if (p.charAt(0) !== '/') p = '/' + p;
        return base() + p;
    };
    try {
        if (!base()) return;
        function rewrite() {
            document.querySelectorAll('a[href^="/app/"]').forEach(function (a) {
                a.setAttribute('href', base() + a.getAttribute('href').slice(4));
            });
        }
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', rewrite);
        } else {
            rewrite();
        }
    } catch (e) { /* ignore */ }
})();
