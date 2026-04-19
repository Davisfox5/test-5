export default function InteractionsPage() {
    return (
        <div className="space-y-4">
            <header>
                <h2 className="text-2xl font-bold">Interactions</h2>
                <p className="text-text-muted mt-1">
                    Here&apos;s your week at a glance — I&apos;ve flagged the calls
                    worth a second look.
                </p>
            </header>
            <div className="rounded-lg border border-border border-dashed bg-bg-card p-8 text-center text-text-subtle">
                Port from{" "}
                <code className="text-text-muted">website/demo.html#interactions</code>{" "}
                with real data from <code className="text-text-muted">/api/v1/interactions</code>.
            </div>
        </div>
    );
}
