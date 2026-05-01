"use client";

import Link from "next/link";
import {
    useEffect,
    useMemo,
    useRef,
    useState,
    type FormEvent,
} from "react";

import {
    formatElapsed,
    speakerLabel,
    useCoachingSessions,
    useLiveSession,
    useMintTicket,
    type CoachingSessionRow,
    type ConnectionStatus,
    type OutboundTagEvent,
    type SuggestionCard,
    type TicketResponse,
    type TranscriptLine,
} from "@/lib/live-coaching";
import { useInteractions } from "@/lib/interactions";
import { useApi } from "@/lib/api";
import { useMe } from "@/lib/me";
import { useQuery } from "@tanstack/react-query";

type InteractionSource = "live_phone" | "live_web" | "replay";
type TagCategory = OutboundTagEvent["category"];

interface UserLookupRow {
    id: string;
    name: string | null;
}

const SOURCE_LABELS: Record<InteractionSource, string> = {
    live_phone: "Live phone call",
    live_web: "Live web call",
    replay: "Replay an interaction",
};

const TAG_BUTTONS: { key: TagCategory; label: string }[] = [
    { key: "question", label: "Question" },
    { key: "coaching", label: "Coaching" },
    { key: "praise", label: "Praise" },
    { key: "issue", label: "Issue" },
];

function useUsersLookup() {
    const api = useApi();
    return useQuery({
        queryKey: ["users-lookup"],
        queryFn: () => api.get<UserLookupRow[]>("/users/lookup"),
    });
}

export default function CoachingPage() {
    const me = useMe();
    const ticketMutation = useMintTicket();
    const [issued, setIssued] = useState<TicketResponse | null>(null);
    const [agentName, setAgentName] = useState<string | null>(null);

    const session = useLiveSession({
        ticket: issued?.ticket ?? null,
        sessionId: issued?.session_id ?? null,
        // /coaching is manager+; we always open the monitor side of the
        // bidi channel so the manager observes whatever room the agent
        // is in. The form's "interaction source" picker only governs
        // *which* session we're observing, not the role we connect as.
        role: "monitor",
    });

    const isActive =
        !!issued &&
        (session.status === "connecting" ||
            session.status === "live" ||
            session.status === "reconnecting");

    return (
        <div className="space-y-6">
            <header className="flex flex-wrap items-end justify-between gap-3">
                <div>
                    <h2 className="text-2xl font-bold">Live Coaching</h2>
                    <p className="text-text-muted mt-1">
                        Listen in on a live call, watch Linda&apos;s suggestions
                        in real time, and tag moments worth a follow-up.
                    </p>
                </div>
                {isActive ? (
                    <SessionHeaderStrip
                        status={session.status}
                        elapsedMs={session.elapsedMs}
                        agentName={agentName}
                        onEnd={() => {
                            session.close();
                        }}
                    />
                ) : null}
            </header>

            {!isActive ? (
                <IdleLayout
                    pending={ticketMutation.isPending}
                    error={ticketMutation.error}
                    sessionStatus={session.status}
                    sessionError={session.error}
                    me={me.data}
                    onStart={async ({ agentId, agentDisplay, source, interactionId }) => {
                        const userId = me.data?.user?.id;
                        const payload: {
                            role: "monitor";
                            user_id?: string;
                            session_id?: string;
                        } = { role: "monitor" };
                        if (userId) payload.user_id = userId;
                        if (source === "replay" && interactionId) {
                            // Replay sessions reuse the interaction id as
                            // the session id so the manager joins the
                            // call's own monitor channel rather than a
                            // fresh empty one.
                            payload.session_id = interactionId;
                        }
                        const out = await ticketMutation.mutateAsync(payload);
                        setIssued(out);
                        setAgentName(agentDisplay ?? agentId);
                    }}
                    onClearExpired={() => setIssued(null)}
                />
            ) : (
                <ActiveLayout
                    transcript={session.transcript}
                    suggestions={session.suggestions}
                    onSendTag={(category, note) =>
                        session.send({ type: "tag", category, note })
                    }
                />
            )}
        </div>
    );
}

/* ── Idle layout ─────────────────────────────────────────────────── */

interface IdleProps {
    pending: boolean;
    error: unknown;
    sessionStatus: ConnectionStatus;
    sessionError: string | null;
    me: ReturnType<typeof useMe>["data"];
    onStart: (args: {
        agentId: string;
        agentDisplay: string | null;
        source: InteractionSource;
        interactionId?: string;
    }) => Promise<void> | void;
    onClearExpired: () => void;
}

function IdleLayout(props: IdleProps) {
    const users = useUsersLookup();
    const [agentId, setAgentId] = useState("");
    const [source, setSource] = useState<InteractionSource>("live_phone");
    const [interactionId, setInteractionId] = useState("");
    const interactionsQuery = useInteractions({ limit: 25 });
    const sessions = useCoachingSessions(15);

    const showReplayPicker = source === "replay";
    const replayInteractions = useMemo(
        () => (interactionsQuery.data ?? []).filter((i) => i.channel === "voice"),
        [interactionsQuery.data],
    );

    const canStart =
        agentId.length > 0 &&
        (source !== "replay" || interactionId.length > 0) &&
        !props.pending;

    const submit = (e: FormEvent) => {
        e.preventDefault();
        const display =
            users.data?.find((u) => u.id === agentId)?.name ?? null;
        props.onStart({
            agentId,
            agentDisplay: display,
            source,
            interactionId: showReplayPicker ? interactionId : undefined,
        });
    };

    return (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <section className="rounded-lg border border-border bg-bg-card p-5 space-y-4">
                <div>
                    <h3 className="text-lg font-semibold">
                        Start a coaching session.
                    </h3>
                    <p className="text-sm text-text-muted mt-1">
                        Pick an agent and an interaction source — Linda
                        will start streaming once the WebSocket opens.
                    </p>
                </div>

                <form onSubmit={submit} className="space-y-3">
                    <label className="block text-sm space-y-1">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Agent
                        </span>
                        <select
                            value={agentId}
                            onChange={(e) => setAgentId(e.target.value)}
                            className="w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm"
                            required
                        >
                            <option value="">Select an agent…</option>
                            {(users.data ?? []).map((u) => (
                                <option key={u.id} value={u.id}>
                                    {u.name ?? u.id}
                                </option>
                            ))}
                        </select>
                    </label>

                    <label className="block text-sm space-y-1">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Interaction source
                        </span>
                        <select
                            value={source}
                            onChange={(e) =>
                                setSource(e.target.value as InteractionSource)
                            }
                            className="w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm"
                        >
                            {Object.entries(SOURCE_LABELS).map(([k, label]) => (
                                <option key={k} value={k}>
                                    {label}
                                </option>
                            ))}
                        </select>
                    </label>

                    {showReplayPicker ? (
                        <label className="block text-sm space-y-1">
                            <span className="text-xs uppercase tracking-wide text-text-subtle">
                                Interaction
                            </span>
                            <select
                                value={interactionId}
                                onChange={(e) =>
                                    setInteractionId(e.target.value)
                                }
                                className="w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm"
                                required
                            >
                                <option value="">Pick an interaction…</option>
                                {replayInteractions.map((i) => (
                                    <option key={i.id} value={i.id}>
                                        {i.title ??
                                            `Call from ${new Date(i.created_at).toLocaleString()}`}
                                    </option>
                                ))}
                            </select>
                        </label>
                    ) : null}

                    <button
                        type="submit"
                        disabled={!canStart}
                        className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                    >
                        {props.pending ? "Starting…" : "Start"}
                    </button>
                </form>

                {props.error ? (
                    <div className="rounded-md border border-accent-rose/40 bg-accent-rose/10 p-3 text-xs text-accent-rose">
                        Couldn&apos;t mint a ticket:{" "}
                        {(props.error as Error).message}
                    </div>
                ) : null}

                {props.sessionStatus === "expired" ? (
                    <div className="rounded-md border border-accent-amber/40 bg-accent-amber/10 p-3 text-xs text-accent-amber space-y-2">
                        <div className="font-medium">Session expired.</div>
                        <p>
                            The ticket Linda issued has been consumed or
                            timed out. Click below to mint a fresh one and
                            try again.
                        </p>
                        <button
                            type="button"
                            onClick={props.onClearExpired}
                            className="rounded-md border border-accent-amber/40 px-3 py-1 hover:bg-accent-amber/10"
                        >
                            Get new ticket
                        </button>
                    </div>
                ) : null}

                {props.sessionStatus === "error" && props.sessionError ? (
                    <div className="rounded-md border border-accent-rose/40 bg-accent-rose/10 p-3 text-xs text-accent-rose">
                        {props.sessionError}
                    </div>
                ) : null}
            </section>

            <section className="rounded-lg border border-border bg-bg-card overflow-hidden">
                <div className="px-5 py-4 border-b border-border flex items-baseline justify-between">
                    <h3 className="text-lg font-semibold">Recent sessions</h3>
                    <span className="text-xs text-text-subtle">
                        {sessions.data?.total ?? 0} total
                    </span>
                </div>
                {sessions.isLoading && !sessions.data ? (
                    <div className="px-5 py-4 text-sm text-text-muted">
                        Loading…
                    </div>
                ) : sessions.error ? (
                    <div className="px-5 py-4 text-sm text-accent-rose">
                        Couldn&apos;t load sessions:{" "}
                        {(sessions.error as Error).message}
                    </div>
                ) : (sessions.data?.items?.length ?? 0) === 0 ? (
                    <div className="px-5 py-6 text-sm text-text-muted">
                        No coaching sessions yet — start one to the left
                        and the recent list will fill in.
                    </div>
                ) : (
                    <ul className="divide-y divide-border">
                        {sessions.data!.items.map((s) => (
                            <SessionRow key={s.id} session={s} />
                        ))}
                    </ul>
                )}
            </section>
        </div>
    );
}

function SessionRow({ session }: { session: CoachingSessionRow }) {
    const dt = new Date(session.started_at);
    const dur =
        session.duration_seconds != null
            ? `${Math.floor(session.duration_seconds / 60)}m ${session.duration_seconds % 60}s`
            : session.status === "active"
              ? "live"
              : "—";
    return (
        <li className="px-5 py-3 flex items-center justify-between gap-4">
            <div className="min-w-0">
                <div className="text-sm font-medium truncate">
                    {session.interaction_title ??
                        session.agent_name ??
                        "Coaching session"}
                </div>
                <div className="text-xs text-text-muted">
                    {session.agent_name ? `${session.agent_name} • ` : ""}
                    {dt.toLocaleString()} • {dur}
                </div>
            </div>
            <div className="flex items-center gap-2">
                <StatusPill status={session.status} />
                {session.interaction_id ? (
                    <Link
                        href={`/interactions/${session.interaction_id}`}
                        className="rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-secondary"
                    >
                        Open call
                    </Link>
                ) : null}
            </div>
        </li>
    );
}

function StatusPill({ status }: { status: string }) {
    const tone =
        status === "active"
            ? "bg-accent-emerald/10 text-accent-emerald border-accent-emerald/30"
            : status === "completed"
              ? "bg-bg-secondary text-text-muted border-border"
              : "bg-bg-secondary text-text-subtle border-border";
    return (
        <span
            className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize ${tone}`}
        >
            {status}
        </span>
    );
}

/* ── Active layout ───────────────────────────────────────────────── */

interface ActiveProps {
    transcript: TranscriptLine[];
    suggestions: SuggestionCard[];
    onSendTag: (category: TagCategory, note: string) => void;
}

function ActiveLayout(props: ActiveProps) {
    return (
        <div className="grid gap-4 md:grid-cols-5">
            <TranscriptPanel transcript={props.transcript} />
            <SuggestionsPanel suggestions={props.suggestions} />
            <div className="md:col-span-5">
                <TaggingComposer onSend={props.onSendTag} />
            </div>
        </div>
    );
}

function TranscriptPanel({ transcript }: { transcript: TranscriptLine[] }) {
    const scrollRef = useRef<HTMLDivElement | null>(null);
    const [autoscroll, setAutoscroll] = useState(true);
    const [jumped, setJumped] = useState(false);

    useEffect(() => {
        const el = scrollRef.current;
        if (!el) return;
        if (!autoscroll) return;
        // Stick to the bottom on every new line. The "jumped" pill is
        // surfaced only when the user manually scrolls up — that flips
        // ``autoscroll`` to false in the scroll handler below.
        el.scrollTop = el.scrollHeight;
    }, [transcript.length, autoscroll]);

    return (
        <section className="md:col-span-3 rounded-lg border border-border bg-bg-card overflow-hidden">
            <div className="px-4 py-2 border-b border-border flex items-center justify-between">
                <h3 className="text-sm font-semibold">Transcript</h3>
                {jumped && !autoscroll ? (
                    <button
                        type="button"
                        onClick={() => {
                            setAutoscroll(true);
                            setJumped(false);
                        }}
                        className="rounded-full bg-primary/10 text-primary px-3 py-0.5 text-xs hover:bg-primary/20"
                    >
                        Jump to live
                    </button>
                ) : null}
            </div>
            <div
                ref={scrollRef}
                onScroll={(e) => {
                    const el = e.currentTarget;
                    const atBottom =
                        el.scrollHeight - el.scrollTop - el.clientHeight < 40;
                    setAutoscroll(atBottom);
                    setJumped(!atBottom);
                }}
                className="h-[60vh] overflow-y-auto px-4 py-3 space-y-2 text-sm"
            >
                {transcript.length === 0 ? (
                    <div className="text-text-muted">
                        Waiting for the first turn…
                    </div>
                ) : (
                    transcript.map((line) => (
                        <div
                            key={line.id}
                            className={`leading-relaxed ${
                                line.isFinal ? "text-text" : "text-text-muted italic"
                            }`}
                        >
                            <span className="text-xs font-semibold uppercase tracking-wide text-text-subtle mr-2">
                                {speakerLabel(line.speaker)}
                            </span>
                            {line.text}
                        </div>
                    ))
                )}
            </div>
        </section>
    );
}

function SuggestionsPanel({ suggestions }: { suggestions: SuggestionCard[] }) {
    return (
        <section className="md:col-span-2 rounded-lg border border-border bg-bg-card overflow-hidden">
            <div className="px-4 py-2 border-b border-border flex items-center justify-between">
                <h3 className="text-sm font-semibold">Live suggestions</h3>
                <span className="text-xs text-text-subtle">
                    {suggestions.length} active
                </span>
            </div>
            <div className="h-[60vh] overflow-y-auto px-3 py-3 space-y-2">
                {suggestions.length === 0 ? (
                    <div className="text-text-muted px-1 py-2 text-sm">
                        No suggestions yet — Linda will surface them as
                        the call progresses.
                    </div>
                ) : (
                    suggestions.map((s) => (
                        <SuggestionCardView key={s.id} card={s} />
                    ))
                )}
            </div>
        </section>
    );
}

function severityClass(severity: SuggestionCard["severity"]): string {
    if (severity === "critical")
        return "border-accent-rose/40 bg-accent-rose/5";
    if (severity === "warn")
        return "border-accent-amber/40 bg-accent-amber/5";
    return "border-border bg-bg-elevated";
}

function categoryIcon(category: string): string {
    switch (category) {
        case "objection":
            return "!";
        case "competitor":
            return "C";
        case "next_step":
        case "next-step":
        case "next-step-required":
            return "→";
        case "compliance":
            return "✓";
        case "sentiment":
        case "sentiment_drop":
            return "♥";
        case "kb":
            return "?";
        case "churn":
            return "⚠";
        case "upsell":
            return "$";
        case "escalation":
            return "↑";
        case "advocate":
            return "★";
        default:
            return "•";
    }
}

function SuggestionCardView({ card }: { card: SuggestionCard }) {
    const ageSec = Math.max(0, Math.floor((Date.now() - card.receivedAt) / 1000));
    const fade = ageSec > 50 ? "opacity-50" : "";

    return (
        <article
            className={`rounded-md border px-3 py-2 transition-opacity ${severityClass(
                card.severity,
            )} ${fade}`}
        >
            <div className="flex items-start gap-2">
                <span
                    className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-bg-secondary text-xs font-semibold text-text-muted"
                    aria-hidden
                >
                    {categoryIcon(card.category)}
                </span>
                <div className="min-w-0 flex-1">
                    <div className="text-xs uppercase tracking-wide text-text-subtle">
                        {card.category.replace(/_/g, " ")}
                    </div>
                    <p className="text-sm mt-0.5">{card.message}</p>
                    {card.sourceDocTitle ? (
                        <p className="text-xs text-text-muted mt-1">
                            Source: {card.sourceDocTitle}
                        </p>
                    ) : null}
                </div>
            </div>
        </article>
    );
}

/* ── Footer composer ─────────────────────────────────────────────── */

function TaggingComposer({
    onSend,
}: {
    onSend: (category: TagCategory, note: string) => void;
}) {
    const [note, setNote] = useState("");
    const [pending, setPending] = useState<TagCategory | null>(null);
    const submit = (category: TagCategory) => {
        const trimmed = note.trim();
        if (!trimmed) return;
        onSend(category, trimmed);
        setNote("");
        setPending(category);
        setTimeout(() => setPending(null), 600);
    };
    return (
        <section className="rounded-lg border border-border bg-bg-card p-4">
            <div className="text-xs uppercase tracking-wide text-text-subtle mb-2">
                Quick tag
            </div>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                <input
                    type="text"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="Note this moment for the post-call review…"
                    className="flex-1 rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm"
                />
                <div className="flex flex-wrap gap-2">
                    {TAG_BUTTONS.map((b) => (
                        <button
                            key={b.key}
                            type="button"
                            onClick={() => submit(b.key)}
                            disabled={!note.trim() || pending === b.key}
                            className="rounded-md border border-border px-3 py-2 text-sm hover:bg-bg-secondary disabled:opacity-50"
                        >
                            {pending === b.key ? "Sent" : b.label}
                        </button>
                    ))}
                </div>
            </div>
        </section>
    );
}

/* ── Header strip ────────────────────────────────────────────────── */

function SessionHeaderStrip({
    status,
    elapsedMs,
    agentName,
    onEnd,
}: {
    status: ConnectionStatus;
    elapsedMs: number;
    agentName: string | null;
    onEnd: () => void;
}) {
    const tone =
        status === "live"
            ? "bg-accent-emerald/10 text-accent-emerald border-accent-emerald/30"
            : status === "connecting"
              ? "bg-bg-secondary text-text-muted border-border"
              : status === "reconnecting"
                ? "bg-accent-amber/10 text-accent-amber border-accent-amber/30"
                : "bg-bg-secondary text-text-muted border-border";
    return (
        <div className="flex items-center gap-3 text-sm">
            <span
                className={`inline-flex items-center rounded-full border px-3 py-0.5 text-xs font-medium capitalize ${tone}`}
            >
                {status}
            </span>
            <span className="text-text-muted">
                {formatElapsed(elapsedMs)} elapsed
            </span>
            {agentName ? (
                <span className="text-text-muted">• {agentName}</span>
            ) : null}
            <button
                type="button"
                onClick={onEnd}
                className="rounded-md border border-accent-rose/40 px-3 py-1 text-xs text-accent-rose hover:bg-accent-rose/10"
            >
                End session
            </button>
        </div>
    );
}
