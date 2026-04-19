/* Session auth bootstrapper.
 *
 * Runs before the rest of the dashboard JS. Responsibilities:
 *
 *   - Pick up a per-user session JWT (or tenant-wide API key) from either
 *     localStorage (repeat visits) or an ?api_key=… query param (admin
 *     onboarding link).
 *   - Call GET /auth/me to resolve the caller's identity + role.
 *   - When no token is set, or /auth/me returns 401, show the login
 *     overlay. On successful login, store the token and reload.
 *   - Stamp the resolved role on <body data-user-role="…"> so CSS can hide
 *     admin-only nav items for non-admins.
 *
 * Compatibility: we keep the existing `callsight-api-key` localStorage
 * slot so legacy controllers (kb-cards, customer-brief, tenant-settings,
 * onboarding) read the session JWT from the same place without any
 * changes on their side.
 */

(function () {
    const API_BASE = window.__CALLSIGHT_API_BASE__ || "/api/v1";
    const TOKEN_KEY = "callsight-api-key";

    function getStoredToken() {
        return localStorage.getItem(TOKEN_KEY);
    }

    function storeToken(t) {
        if (!t) localStorage.removeItem(TOKEN_KEY);
        else localStorage.setItem(TOKEN_KEY, t);
    }

    // Admins can drop a ?api_key=csk_… into the URL on first onboarding so
    // the UI can reach /auth/me without a session. We persist it to
    // localStorage and strip it from the URL so it's not bookmarkable.
    function maybePickupQueryKey() {
        try {
            const url = new URL(window.location.href);
            const key = url.searchParams.get("api_key") || url.searchParams.get("token");
            if (key) {
                storeToken(key);
                url.searchParams.delete("api_key");
                url.searchParams.delete("token");
                window.history.replaceState({}, "", url.toString());
            }
        } catch (e) { /* older browsers: ignore */ }
    }

    async function fetchMe() {
        const token = getStoredToken();
        if (!token) return null;
        try {
            const resp = await fetch(`${API_BASE}/auth/me`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (resp.status === 401) return null;
            if (!resp.ok) return null;
            return await resp.json();
        } catch (err) {
            console.warn("auth/me failed", err);
            return null;
        }
    }

    function showLogin() {
        const overlay = document.getElementById("loginOverlay");
        if (overlay) overlay.hidden = false;
    }

    function hideLogin() {
        const overlay = document.getElementById("loginOverlay");
        if (overlay) overlay.hidden = true;
    }

    function applyIdentity(me) {
        window.callsightAuth = me;
        if (!me) {
            document.body.removeAttribute("data-user-role");
            return;
        }
        document.body.setAttribute("data-user-role", me.role || "agent");
        // Tell existing modules which key to use (already the same slot,
        // but some controllers read this explicit global).
        window.__CALLSIGHT_API_TOKEN__ = getStoredToken();
    }

    async function tryLogin(email, password) {
        const resp = await fetch(`${API_BASE}/auth/login`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email, password }),
        });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.detail || `Login failed (${resp.status})`);
        }
        const { token, user } = await resp.json();
        storeToken(token);
        return user;
    }

    function wireLoginForm() {
        const form = document.getElementById("loginForm");
        if (!form) return;
        const error = document.getElementById("loginError");
        const submit = document.getElementById("loginSubmit");

        form.addEventListener("submit", async (ev) => {
            ev.preventDefault();
            if (error) error.hidden = true;
            submit.disabled = true;
            submit.textContent = "Signing in…";
            try {
                const email = document.getElementById("loginEmail").value.trim();
                const password = document.getElementById("loginPassword").value;
                await tryLogin(email, password);
                // Re-fetch identity with the new token.
                const me = await fetchMe();
                applyIdentity(me);
                hideLogin();
            } catch (err) {
                if (error) {
                    error.textContent = err.message || "Login failed.";
                    error.hidden = false;
                }
            } finally {
                submit.disabled = false;
                submit.textContent = "Sign in";
            }
        });
    }

    async function bootstrap() {
        maybePickupQueryKey();
        wireLoginForm();

        const me = await fetchMe();
        if (me) {
            applyIdentity(me);
            hideLogin();
        } else {
            applyIdentity(null);
            showLogin();
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
