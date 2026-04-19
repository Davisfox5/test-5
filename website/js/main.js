// ======== MIGRATION: one-time rename of legacy callsight-* localStorage keys ========
(function migrateLegacyStorageKeys() {
    try {
        var legacyPrefix = 'callsight-';
        var newPrefix = 'linda-';
        var legacy = [];
        for (var i = 0; i < localStorage.length; i++) {
            var k = localStorage.key(i);
            if (k && k.indexOf(legacyPrefix) === 0) legacy.push(k);
        }
        legacy.forEach(function(k) {
            var newKey = newPrefix + k.slice(legacyPrefix.length);
            if (localStorage.getItem(newKey) === null) {
                localStorage.setItem(newKey, localStorage.getItem(k));
            }
            localStorage.removeItem(k);
        });
    } catch (e) { /* ignore */ }
})();

// ======== THEME (runs before DOMContentLoaded to avoid flash) ========
(function initTheme() {
    try {
        var stored = localStorage.getItem('linda-theme');
        var prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
        var theme = stored || (prefersLight ? 'light' : 'dark');
        if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
    } catch (e) { /* ignore */ }
})();

document.addEventListener('DOMContentLoaded', () => {
    // ======== NAVBAR SCROLL EFFECT ========
    const navbar = document.getElementById('navbar');
    if (navbar) {
        window.addEventListener('scroll', () => {
            if (window.scrollY > 50) {
                navbar.classList.add('scrolled');
            } else {
                navbar.classList.remove('scrolled');
            }
        }, { passive: true });
    }

    // ======== MOBILE NAV TOGGLE ========
    const navToggle = document.getElementById('navToggle');
    const navLinks = document.getElementById('navLinks');
    const navScrim = document.getElementById('navScrim');

    function setNavOpen(open) {
        if (!navToggle || !navLinks) return;
        navLinks.classList.toggle('active', open);
        navToggle.classList.toggle('active', open);
        if (navScrim) navScrim.classList.toggle('active', open);
        navToggle.setAttribute('aria-expanded', String(open));
        navToggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
        document.body.style.overflow = open ? 'hidden' : '';
    }

    if (navToggle) {
        navToggle.addEventListener('click', () => {
            const isOpen = navLinks.classList.contains('active');
            setNavOpen(!isOpen);
        });
    }
    if (navScrim) {
        navScrim.addEventListener('click', () => setNavOpen(false));
    }
    if (navLinks) {
        navLinks.querySelectorAll('a').forEach((a) => {
            a.addEventListener('click', () => setNavOpen(false));
        });
    }
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && navLinks && navLinks.classList.contains('active')) {
            setNavOpen(false);
            if (navToggle) navToggle.focus();
        }
    });

    // ======== THEME TOGGLE ========
    const themeToggle = document.getElementById('themeToggle');
    if (themeToggle) {
        themeToggle.addEventListener('click', () => {
            const isLight = document.documentElement.getAttribute('data-theme') === 'light';
            const next = isLight ? 'dark' : 'light';
            if (next === 'light') {
                document.documentElement.setAttribute('data-theme', 'light');
            } else {
                document.documentElement.removeAttribute('data-theme');
            }
            try { localStorage.setItem('linda-theme', next); } catch (e) {}
        });
    }

    // ======== COUNT UP ANIMATION FOR STATS ========
    const stats = document.querySelectorAll('.stat-number');
    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    if (stats.length) {
        const statsObserver = new IntersectionObserver((entries, observer) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    const target = parseFloat(entry.target.getAttribute('data-target'));
                    if (prefersReducedMotion) {
                        entry.target.innerHTML = Number.isInteger(target) ? target : target.toFixed(1);
                    } else {
                        animateValue(entry.target, 0, target, 2000);
                    }
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: 0.5 });
        stats.forEach((stat) => statsObserver.observe(stat));
    }

    function animateValue(obj, start, end, duration) {
        let startTimestamp = null;
        const step = (timestamp) => {
            if (!startTimestamp) startTimestamp = timestamp;
            const progress = Math.min((timestamp - startTimestamp) / duration, 1);
            const val = progress * (end - start) + start;
            obj.innerHTML = Number.isInteger(end) ? Math.floor(val) : val.toFixed(1);
            if (progress < 1) window.requestAnimationFrame(step);
        };
        window.requestAnimationFrame(step);
    }

    // ======== CONTACT FORM (with validation) ========
    const contactForm = document.getElementById('contactForm');
    if (contactForm) {
        const nameInput = document.getElementById('contactName');
        const emailInput = document.getElementById('contactEmail');
        const errName = document.getElementById('err-name');
        const errEmail = document.getElementById('err-email');
        const statusEl = document.getElementById('formStatus');
        const submitBtn = contactForm.querySelector('button[type="submit"]');

        function setError(input, errEl, message) {
            if (!input || !errEl) return;
            if (message) {
                input.setAttribute('aria-invalid', 'true');
                errEl.textContent = message;
            } else {
                input.removeAttribute('aria-invalid');
                errEl.textContent = '';
            }
        }

        function validateName() {
            const v = (nameInput.value || '').trim();
            if (!v) { setError(nameInput, errName, 'Please enter your name.'); return false; }
            setError(nameInput, errName, ''); return true;
        }
        function validateEmail() {
            const v = (emailInput.value || '').trim();
            if (!v) { setError(emailInput, errEmail, 'Please enter your work email.'); return false; }
            const re = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
            if (!re.test(v)) { setError(emailInput, errEmail, 'Please enter a valid email address.'); return false; }
            setError(emailInput, errEmail, ''); return true;
        }

        nameInput && nameInput.addEventListener('blur', validateName);
        emailInput && emailInput.addEventListener('blur', validateEmail);
        nameInput && nameInput.addEventListener('input', () => {
            if (nameInput.getAttribute('aria-invalid') === 'true') validateName();
        });
        emailInput && emailInput.addEventListener('input', () => {
            if (emailInput.getAttribute('aria-invalid') === 'true') validateEmail();
        });

        contactForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const ok = [validateName(), validateEmail()].every(Boolean);
            if (statusEl) { statusEl.hidden = true; statusEl.className = 'form-status'; statusEl.textContent = ''; }
            if (!ok) {
                const firstInvalid = contactForm.querySelector('[aria-invalid="true"]');
                if (firstInvalid) firstInvalid.focus();
                return;
            }

            submitBtn.disabled = true;
            const original = submitBtn.textContent;
            submitBtn.textContent = 'Sending…';

            // Simulated API call
            setTimeout(() => {
                submitBtn.disabled = false;
                submitBtn.textContent = original;
                contactForm.reset();
                if (statusEl) {
                    statusEl.hidden = false;
                    statusEl.className = 'form-status success';
                    statusEl.textContent = "Thanks! We've received your application and will reach out within 2 business days.";
                }
            }, 1200);
        });
    }

    // ======== REVEAL ON SCROLL ========
    if (!prefersReducedMotion) {
        const revealElements = document.querySelectorAll('.feature-card, .step, .pricing-card, .integration-card, .security-item, .preview-phase');
        const revealObserver = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    entry.target.style.opacity = '1';
                    entry.target.style.transform = 'translateY(0)';
                }
            });
        }, { threshold: 0.1 });

        revealElements.forEach((el) => {
            el.style.opacity = '0';
            el.style.transform = 'translateY(30px)';
            el.style.transition = 'opacity 0.6s ease-out, transform 0.6s ease-out';
            revealObserver.observe(el);
        });
    }
});
