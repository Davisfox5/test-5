import type { Config } from "tailwindcss";

/*
 * Tailwind preset for the Tier 2/3 SPA.
 *
 * Every color / radius / shadow / font token maps to a CSS custom
 * property defined in website/css/tokens.css (imported by globals.css
 * via a relative path). That means Tailwind utilities, the vanilla
 * public demo, and the Ask-Linda widget all resolve to the same design
 * tokens — themes switch by toggling the [data-theme] attribute once.
 */
const config: Config = {
    content: [
        "./src/app/**/*.{ts,tsx}",
        "./src/components/**/*.{ts,tsx}",
        "./src/lib/**/*.{ts,tsx}",
    ],
    theme: {
        extend: {
            colors: {
                bg: {
                    main: "var(--bg-main)",
                    secondary: "var(--bg-secondary)",
                    card: "var(--bg-card)",
                    "card-hover": "var(--bg-card-hover)",
                },
                text: {
                    DEFAULT: "var(--text-main)",
                    muted: "var(--text-muted)",
                    subtle: "var(--text-subtle)",
                },
                primary: {
                    DEFAULT: "var(--primary)",
                    hover: "var(--primary-hover)",
                    soft: "var(--primary-soft)",
                },
                secondary: "var(--secondary)",
                accent: {
                    emerald: "var(--accent-emerald)",
                    rose: "var(--accent-rose)",
                    amber: "var(--accent-amber)",
                    cyan: "var(--accent-cyan)",
                },
                border: {
                    DEFAULT: "var(--border)",
                    light: "var(--border-light)",
                    strong: "var(--border-strong)",
                },
            },
            borderRadius: {
                sm: "var(--radius-sm)",
                md: "var(--radius-md)",
                lg: "var(--radius-lg)",
                xl: "var(--radius-xl)",
            },
            boxShadow: {
                sm: "var(--shadow-sm)",
                md: "var(--shadow-md)",
                lg: "var(--shadow-lg)",
                xl: "var(--shadow-xl)",
            },
            fontFamily: {
                sans: ["var(--font-sans)"],
                mono: ["var(--font-mono)"],
            },
            backgroundImage: {
                brand: "var(--brand-grad)",
            },
        },
    },
    plugins: [],
};

export default config;
