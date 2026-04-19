export default function ActionItemsPage() {
    return (
        <div className="space-y-4">
            <header>
                <h2 className="text-2xl font-bold">Action items</h2>
                <p className="text-text-muted mt-1">
                    Everything Linda pulled out of your calls — assign, snooze, or close.
                </p>
            </header>
            <div className="rounded-lg border border-border border-dashed bg-bg-card p-8 text-center text-text-subtle">
                Port from <code className="text-text-muted">website/demo.html#action-items</code>{" "}
                with live data from <code className="text-text-muted">/api/v1/action-items</code>.
            </div>
        </div>
    );
}
