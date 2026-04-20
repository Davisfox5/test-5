export function LindaWordmark({ className = "" }: { className?: string }) {
    return (
        <svg viewBox="0 0 260 72" role="img" aria-label="LINDA" className={className}>
            <defs>
                <linearGradient id="linda-wordmark-grad-spa" x1="0" y1="0" x2="1" y2="0">
                    <stop offset="0%" stopColor="#6366F1" />
                    <stop offset="100%" stopColor="#8B5CF6" />
                </linearGradient>
            </defs>
            <text
                x="130"
                y="56"
                textAnchor="middle"
                fontFamily="var(--font-sans)"
                fontWeight={900}
                fontSize={64}
                letterSpacing={2}
                fill="url(#linda-wordmark-grad-spa)"
            >
                LINDA
            </text>
        </svg>
    );
}
