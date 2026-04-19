export function LindaMark({ className = "", size = 28 }: { className?: string; size?: number }) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 32 32"
            fill="none"
            aria-hidden="true"
            className={className}
        >
            <defs>
                <linearGradient id="linda-mark-grad-spa" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0%" stopColor="#6366F1" />
                    <stop offset="100%" stopColor="#8B5CF6" />
                </linearGradient>
                <linearGradient id="linda-mark-grad-spa-ghost" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0%" stopColor="#6366F1" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#8B5CF6" stopOpacity={0.3} />
                </linearGradient>
            </defs>
            <ellipse cx={10} cy={20} rx={6} ry={7} fill="url(#linda-mark-grad-spa-ghost)" />
            <ellipse cx={19} cy={15} rx={9} ry={11} fill="url(#linda-mark-grad-spa)" />
        </svg>
    );
}
