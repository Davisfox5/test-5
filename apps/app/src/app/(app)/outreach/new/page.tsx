"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
    FONT_SIZES,
    FONT_STACKS,
    MarkerEditor,
} from "@/components/outreach/marker-editor";
import {
    ApiError,
} from "@/lib/api";
import {
    useCreateOutreachCampaign,
    useDeleteEmailLogo,
    useEmailLogo,
    useProspects,
    useUploadEmailLogo,
    useUploadOutreachAttachment,
    type OutreachAttachmentRef,
    type OutreachConfigShape,
    type OutreachStepConfig,
} from "@/lib/outreach";

const FONT_LABELS: Record<string, string> = {
    arial: "Arial",
    helvetica: "Helvetica",
    georgia: "Georgia",
    times: "Times New Roman",
    verdana: "Verdana",
    tahoma: "Tahoma",
    trebuchet: "Trebuchet MS",
    courier: "Courier New",
};

const TIMEZONES: { value: string; label: string }[] = [
    { value: "America/New_York", label: "Eastern (New York)" },
    { value: "America/Chicago", label: "Central (Chicago)" },
    { value: "America/Denver", label: "Mountain (Denver)" },
    { value: "America/Los_Angeles", label: "Pacific (Los Angeles)" },
    { value: "UTC", label: "UTC" },
];

const DAY_CHIPS: { iso: number; label: string }[] = [
    { iso: 1, label: "Mon" },
    { iso: 2, label: "Tue" },
    { iso: 3, label: "Wed" },
    { iso: 4, label: "Thu" },
    { iso: 5, label: "Fri" },
    { iso: 6, label: "Sat" },
    { iso: 7, label: "Sun" },
];

type ProviderChoice = "auto" | "google" | "microsoft";

function hourLabel(h: number, isEnd = false): string {
    if (isEnd && h === 24) return "Midnight (end of day)";
    const hh = ((h % 24) + 24) % 24;
    const period = hh < 12 ? "AM" : "PM";
    const twelve = hh % 12 === 0 ? 12 : hh % 12;
    return `${twelve} ${period}`;
}

function humanSize(bytes: number | null | undefined): string {
    if (bytes == null) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function NewOutreachCampaignPage() {
    const router = useRouter();
    const create = useCreateOutreachCampaign();

    // Campaign
    const [name, setName] = useState("");

    // Email
    const [subject, setSubject] = useState("");
    const [body, setBody] = useState("");
    const [fontFamily, setFontFamily] = useState<string | null>(null);
    const [fontSizePx, setFontSizePx] = useState<number | null>(null);

    // Signature & logo
    const [includeLogo, setIncludeLogo] = useState(true);
    const [senderName, setSenderName] = useState("");
    const [senderBusiness, setSenderBusiness] = useState("");
    const [physicalAddress, setPhysicalAddress] = useState("");
    const logo = useEmailLogo();
    const uploadLogo = useUploadEmailLogo();
    const deleteLogo = useDeleteEmailLogo();
    const logoInputRef = useRef<HTMLInputElement | null>(null);
    const [logoErr, setLogoErr] = useState<string | null>(null);

    // Attachments
    const [attachments, setAttachments] = useState<OutreachAttachmentRef[]>([]);
    const uploadAttachment = useUploadOutreachAttachment();
    const [attachErr, setAttachErr] = useState<string | null>(null);
    const attachInputRef = useRef<HTMLInputElement | null>(null);

    // Sequence
    const [steps, setSteps] = useState<OutreachStepConfig[]>([
        { offset_days: 0 },
        { offset_days: 4, guidance: "Short, friendly bump." },
    ]);
    const [maxTouches, setMaxTouches] = useState(2);
    const [mode, setMode] = useState<"review" | "auto">("review");

    // Delivery
    const [startHour, setStartHour] = useState(9);
    const [endHour, setEndHour] = useState(17);
    const [timezone, setTimezone] = useState("America/New_York");
    const [days, setDays] = useState<number[]>([1, 2, 3, 4, 5]);
    const [dailyLimit, setDailyLimit] = useState("");
    const [provider, setProvider] = useState<ProviderChoice>("auto");

    // Prospects
    const [prospectQ, setProspectQ] = useState("");
    const [selectedProspectIds, setSelectedProspectIds] = useState<Set<string>>(
        new Set(),
    );
    const prospects = useProspects({ limit: 100, q: prospectQ || undefined });
    const prospectItems = prospects.data?.items ?? [];

    const [formErr, setFormErr] = useState<string | null>(null);

    async function handleLogoFile(file: File | null) {
        if (!file) return;
        setLogoErr(null);
        try {
            await uploadLogo.mutateAsync(file);
        } catch (e) {
            setLogoErr(e instanceof Error ? e.message : "Upload failed.");
        }
    }

    async function handleAttachmentFile(file: File | null) {
        if (!file) return;
        setAttachErr(null);
        if (attachments.length >= 5) {
            setAttachErr("Attachments are capped at 5 per campaign.");
            return;
        }
        try {
            const uploaded = await uploadAttachment.mutateAsync(file);
            setAttachments((prev) => [
                ...prev,
                {
                    s3_key: uploaded.s3_key,
                    filename: uploaded.filename,
                    content_type: uploaded.content_type,
                    size_bytes: uploaded.size_bytes,
                },
            ]);
        } catch (e) {
            setAttachErr(e instanceof Error ? e.message : "Upload failed.");
        }
    }

    function removeAttachment(key: string) {
        setAttachments((prev) => prev.filter((a) => a.s3_key !== key));
    }

    function updateStep(idx: number, patch: Partial<OutreachStepConfig>) {
        setSteps((prev) =>
            prev.map((s, i) => (i === idx ? { ...s, ...patch } : s)),
        );
    }

    function addStep() {
        setSteps((prev) =>
            prev.length >= 6
                ? prev
                : [...prev, { offset_days: 4, guidance: "" }],
        );
    }

    function removeStep(idx: number) {
        setSteps((prev) =>
            prev.length > 1 ? prev.filter((_, i) => i !== idx) : prev,
        );
    }

    function toggleDay(iso: number) {
        setDays((prev) => {
            if (prev.includes(iso)) {
                return prev.length > 1 ? prev.filter((d) => d !== iso) : prev;
            }
            return [...prev, iso].sort((a, b) => a - b);
        });
    }

    function toggleProspect(id: string) {
        setSelectedProspectIds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }

    function selectAllShown() {
        setSelectedProspectIds((prev) => {
            const next = new Set(prev);
            for (const p of prospectItems) next.add(p.prospect_id);
            return next;
        });
    }

    function requiredFieldErrors(): string | null {
        if (!name.trim()) return "Campaign name is required.";
        if (!subject.trim()) return "Email subject is required.";
        if (!body.trim()) return "Email body is required.";
        if (!senderName.trim()) return "Sender name is required.";
        if (!senderBusiness.trim()) return "Sender business is required.";
        if (!physicalAddress.trim())
            return "A physical address is required (CAN-SPAM).";
        return null;
    }

    async function handleSubmit() {
        setFormErr(null);
        const guardErr = requiredFieldErrors();
        if (guardErr) {
            setFormErr(guardErr);
            return;
        }

        const config: OutreachConfigShape = {
            template: {
                subject: subject.trim(),
                body,
                sender_name: senderName.trim(),
                sender_business: senderBusiness.trim(),
                physical_address: physicalAddress.trim(),
                // Cast: the <select>'s options are drawn from
                // FONT_STACKS' keys (which line up with EmailFontFamily),
                // but Object.keys() widens them to plain `string`.
                font_family: (fontFamily ||
                    null) as OutreachConfigShape["template"]["font_family"],
                font_size_px: fontSizePx,
                include_logo: includeLogo,
            },
            send_window: {
                start_hour: startHour,
                end_hour: endHour,
                timezone,
                days,
            },
            steps: steps.map((s, idx) => ({
                offset_days: idx === 0 ? 0 : s.offset_days,
                ...(s.guidance && s.guidance.trim()
                    ? { guidance: s.guidance.trim() }
                    : {}),
            })),
            max_touches: maxTouches,
            mode,
            ...(provider !== "auto" ? { provider } : {}),
            attachments,
            ...(dailyLimit.trim() ? { daily_limit: Number(dailyLimit) } : {}),
        };

        try {
            const campaign = await create.mutateAsync({
                name: name.trim(),
                config,
                prospect_ids: Array.from(selectedProspectIds),
            });
            router.push(`/outreach/${campaign.id}`);
        } catch (e) {
            setFormErr(
                e instanceof ApiError
                    ? e.message
                    : e instanceof Error
                      ? e.message
                      : "Couldn't create the campaign.",
            );
        }
    }

    return (
        <div className="space-y-6 pb-24">
            <div>
                <Link
                    href="/outreach"
                    className="text-sm text-primary hover:underline"
                >
                    ← Back to outreach
                </Link>
            </div>

            <header>
                <h2 className="text-2xl font-bold">New outreach campaign</h2>
                <p className="mt-1 text-text-muted">
                    Write one template — Linda personalizes and sequences it
                    per prospect.
                </p>
            </header>

            <Card title="Campaign">
                <Field label="Name">
                    <input
                        type="text"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder="e.g. Q3 gym owners outreach"
                        className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                    />
                </Field>
            </Card>

            <Card title="Email">
                <Field
                    label="Subject"
                    help="Placeholders like {business_name} are substituted per prospect."
                >
                    <input
                        type="text"
                        value={subject}
                        onChange={(e) => setSubject(e.target.value)}
                        placeholder="Quick question for {business_name}"
                        className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                    />
                </Field>

                <div className="flex flex-wrap items-center gap-3">
                    <label className="flex items-center gap-2 text-xs text-text-muted">
                        Font
                        <select
                            value={fontFamily ?? ""}
                            onChange={(e) =>
                                setFontFamily(e.target.value || null)
                            }
                            className="rounded-md border border-border bg-bg-secondary px-2 py-1 text-xs"
                        >
                            <option value="">Default</option>
                            {Object.keys(FONT_STACKS).map((key) => (
                                <option key={key} value={key}>
                                    {FONT_LABELS[key] ?? key}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="flex items-center gap-2 text-xs text-text-muted">
                        Size
                        <select
                            value={fontSizePx ?? ""}
                            onChange={(e) =>
                                setFontSizePx(
                                    e.target.value
                                        ? Number(e.target.value)
                                        : null,
                                )
                            }
                            className="rounded-md border border-border bg-bg-secondary px-2 py-1 text-xs"
                        >
                            <option value="">Default</option>
                            {FONT_SIZES.map((sz) => (
                                <option key={sz} value={sz}>
                                    {sz}px
                                </option>
                            ))}
                        </select>
                    </label>
                </div>

                <Field label="Body">
                    <MarkerEditor
                        value={body}
                        onChange={setBody}
                        fontFamily={fontFamily}
                        fontSizePx={fontSizePx}
                        placeholder="Hi {business_name}, ..."
                        minHeightClass="min-h-[220px]"
                    />
                    <p className="mt-1 text-xs text-text-subtle">
                        Drafts are personalized per prospect from this
                        template; <strong>bold</strong>, <em>italic</em>,{" "}
                        <span className="underline">underline</span>{" "}
                        formatting carries through.
                    </p>
                </Field>
            </Card>

            <Card title="Signature & logo">
                <label className="flex items-center gap-2 text-sm">
                    <input
                        type="checkbox"
                        checked={includeLogo}
                        onChange={(e) => setIncludeLogo(e.target.checked)}
                        className="rounded border-border"
                    />
                    Add your logo at the bottom of every email
                </label>

                {includeLogo ? (
                    <div className="rounded-md border border-border bg-bg-secondary p-3">
                        {logo.isLoading ? (
                            <div className="h-12 w-28 animate-pulse rounded bg-bg-card" />
                        ) : logo.data ? (
                            <div className="flex flex-wrap items-center gap-3">
                                {/* eslint-disable-next-line @next/next/no-img-element */}
                                <img
                                    src={logo.data.url ?? undefined}
                                    alt="Email logo"
                                    className="max-h-12 rounded border border-border bg-white p-1"
                                />
                                <span className="text-xs text-text-muted">
                                    {logo.data.filename}
                                </span>
                                <button
                                    type="button"
                                    onClick={() =>
                                        logoInputRef.current?.click()
                                    }
                                    disabled={uploadLogo.isPending}
                                    className="rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-card-hover disabled:opacity-50"
                                >
                                    Replace
                                </button>
                                <button
                                    type="button"
                                    onClick={() => deleteLogo.mutate()}
                                    disabled={deleteLogo.isPending}
                                    className="rounded-md border border-accent-rose/40 px-2 py-1 text-xs text-accent-rose hover:bg-accent-rose/10 disabled:opacity-50"
                                >
                                    Remove
                                </button>
                            </div>
                        ) : (
                            <div>
                                <p className="text-xs text-text-muted">
                                    No logo uploaded yet — upload one so it
                                    can appear at the bottom of your emails.
                                </p>
                                <input
                                    type="file"
                                    accept="image/png,image/jpeg,image/gif"
                                    onChange={(e) =>
                                        handleLogoFile(
                                            e.target.files?.[0] ?? null,
                                        )
                                    }
                                    className="mt-2 block text-sm text-text-muted file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-2 file:text-sm file:font-medium file:text-white hover:file:bg-primary-hover"
                                />
                            </div>
                        )}
                        <input
                            ref={logoInputRef}
                            type="file"
                            accept="image/png,image/jpeg,image/gif"
                            className="hidden"
                            onChange={(e) =>
                                handleLogoFile(e.target.files?.[0] ?? null)
                            }
                        />
                        {logoErr ? (
                            <p className="mt-2 text-xs text-accent-rose">
                                {logoErr}
                            </p>
                        ) : null}
                    </div>
                ) : null}

                <Field label="Sender name">
                    <input
                        type="text"
                        value={senderName}
                        onChange={(e) => setSenderName(e.target.value)}
                        className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                    />
                </Field>
                <Field label="Sender business">
                    <input
                        type="text"
                        value={senderBusiness}
                        onChange={(e) => setSenderBusiness(e.target.value)}
                        className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                    />
                </Field>
                <Field
                    label="Physical address"
                    help="Appears in the footer of every email — required by CAN-SPAM."
                >
                    <input
                        type="text"
                        value={physicalAddress}
                        onChange={(e) => setPhysicalAddress(e.target.value)}
                        className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                    />
                </Field>
            </Card>

            <Card title="Attachments">
                <div className="space-y-2">
                    {attachments.map((a) => (
                        <div
                            key={a.s3_key}
                            className="flex items-center justify-between rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm"
                        >
                            <span className="truncate">{a.filename}</span>
                            <div className="flex items-center gap-3">
                                <span className="text-xs text-text-subtle">
                                    {humanSize(a.size_bytes)}
                                </span>
                                <button
                                    type="button"
                                    onClick={() => removeAttachment(a.s3_key)}
                                    className="text-xs text-accent-rose hover:underline"
                                >
                                    Remove
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
                {attachments.length < 5 ? (
                    <div>
                        <input
                            ref={attachInputRef}
                            type="file"
                            disabled={uploadAttachment.isPending}
                            onChange={(e) => {
                                handleAttachmentFile(
                                    e.target.files?.[0] ?? null,
                                );
                                if (attachInputRef.current)
                                    attachInputRef.current.value = "";
                            }}
                            className="block text-sm text-text-muted file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-2 file:text-sm file:font-medium file:text-white hover:file:bg-primary-hover"
                        />
                        <p className="mt-1 text-xs text-text-subtle">
                            Up to 5 files, 10 MB each. Attached to every send.
                        </p>
                    </div>
                ) : (
                    <p className="text-xs text-text-subtle">
                        Maximum of 5 attachments reached.
                    </p>
                )}
                {attachErr ? (
                    <p className="text-xs text-accent-rose">{attachErr}</p>
                ) : null}
            </Card>

            <Card title="Sequence">
                <div className="space-y-3">
                    {steps.map((step, idx) =>
                        idx === 0 ? (
                            <div
                                key={idx}
                                className="rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm text-text-muted"
                            >
                                Step 1 — Send immediately on activation
                            </div>
                        ) : (
                            <div
                                key={idx}
                                className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-bg-secondary px-3 py-2"
                            >
                                <span className="text-sm text-text-muted">
                                    Step {idx + 1} — wait
                                </span>
                                <input
                                    type="number"
                                    min={1}
                                    max={90}
                                    value={step.offset_days}
                                    onChange={(e) =>
                                        updateStep(idx, {
                                            offset_days: Number(
                                                e.target.value,
                                            ),
                                        })
                                    }
                                    className="w-16 rounded-md border border-border bg-bg-card px-2 py-1 text-sm outline-none focus:border-primary"
                                />
                                <span className="text-sm text-text-muted">
                                    days
                                </span>
                                <input
                                    type="text"
                                    value={step.guidance ?? ""}
                                    onChange={(e) =>
                                        updateStep(idx, {
                                            guidance: e.target.value,
                                        })
                                    }
                                    placeholder="Optional guidance, e.g. 'Short, friendly bump.'"
                                    className="min-w-[220px] flex-1 rounded-md border border-border bg-bg-card px-2 py-1 text-sm outline-none focus:border-primary"
                                />
                                <button
                                    type="button"
                                    onClick={() => removeStep(idx)}
                                    className="text-xs text-accent-rose hover:underline"
                                >
                                    Remove
                                </button>
                            </div>
                        ),
                    )}
                    {steps.length < 6 ? (
                        <button
                            type="button"
                            onClick={addStep}
                            className="text-xs text-primary hover:underline"
                        >
                            + Add step
                        </button>
                    ) : null}
                </div>

                <div className="mt-4 flex flex-wrap items-center gap-6">
                    <label className="flex items-center gap-2 text-sm">
                        Max touches
                        <input
                            type="number"
                            min={1}
                            max={6}
                            value={maxTouches}
                            onChange={(e) =>
                                setMaxTouches(Number(e.target.value))
                            }
                            className="w-16 rounded-md border border-border bg-bg-secondary px-2 py-1 text-sm outline-none focus:border-primary"
                        />
                    </label>

                    <div className="flex items-center gap-4 text-sm">
                        <label className="flex items-center gap-2">
                            <input
                                type="radio"
                                name="mode"
                                checked={mode === "review"}
                                onChange={() => setMode("review")}
                            />
                            Review drafts before sending
                        </label>
                        <label className="flex items-center gap-2">
                            <input
                                type="radio"
                                name="mode"
                                checked={mode === "auto"}
                                onChange={() => setMode("auto")}
                            />
                            Auto-send as drafts generate
                        </label>
                    </div>
                </div>
            </Card>

            <Card title="Delivery">
                <div className="flex flex-wrap items-end gap-4">
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Start
                        </span>
                        <select
                            value={startHour}
                            onChange={(e) =>
                                setStartHour(Number(e.target.value))
                            }
                            className="rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm"
                        >
                            {Array.from({ length: 24 }, (_, h) => h).map(
                                (h) => (
                                    <option key={h} value={h}>
                                        {hourLabel(h)}
                                    </option>
                                ),
                            )}
                        </select>
                    </label>
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            End
                        </span>
                        <select
                            value={endHour}
                            onChange={(e) =>
                                setEndHour(Number(e.target.value))
                            }
                            className="rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm"
                        >
                            {Array.from({ length: 24 }, (_, i) => i + 1).map(
                                (h) => (
                                    <option key={h} value={h}>
                                        {hourLabel(h, true)}
                                    </option>
                                ),
                            )}
                        </select>
                    </label>
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Timezone
                        </span>
                        <select
                            value={timezone}
                            onChange={(e) => setTimezone(e.target.value)}
                            className="rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm"
                        >
                            {TIMEZONES.map((tz) => (
                                <option key={tz.value} value={tz.value}>
                                    {tz.label}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Daily limit
                        </span>
                        <input
                            type="number"
                            min={1}
                            max={200}
                            value={dailyLimit}
                            onChange={(e) => setDailyLimit(e.target.value)}
                            placeholder="Default"
                            className="w-28 rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                        />
                    </label>
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Send via
                        </span>
                        <select
                            value={provider}
                            onChange={(e) =>
                                setProvider(e.target.value as ProviderChoice)
                            }
                            className="rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm"
                        >
                            <option value="auto">Auto</option>
                            <option value="google">Gmail</option>
                            <option value="microsoft">Outlook</option>
                        </select>
                    </label>
                </div>

                <div>
                    <span className="mb-1 block text-xs uppercase tracking-wide text-text-subtle">
                        Days
                    </span>
                    <div className="flex flex-wrap gap-1.5">
                        {DAY_CHIPS.map((d) => (
                            <button
                                key={d.iso}
                                type="button"
                                onClick={() => toggleDay(d.iso)}
                                className={`rounded-full border px-3 py-1 text-xs transition ${
                                    days.includes(d.iso)
                                        ? "border-primary bg-primary text-white"
                                        : "border-border text-text-muted hover:bg-bg-card-hover"
                                }`}
                            >
                                {d.label}
                            </button>
                        ))}
                    </div>
                </div>
            </Card>

            <Card title="Prospects">
                <p className="text-xs text-text-muted">
                    Enrolling prospects now is optional — you can add more
                    from the campaign page later.
                </p>
                <input
                    type="text"
                    value={prospectQ}
                    onChange={(e) => setProspectQ(e.target.value)}
                    placeholder="Search by business name or domain"
                    className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                />
                <div className="flex items-center justify-between text-xs text-text-muted">
                    <button
                        type="button"
                        onClick={selectAllShown}
                        className="text-primary hover:underline"
                    >
                        Select all shown
                    </button>
                    <span>{selectedProspectIds.size} selected</span>
                </div>
                <div className="max-h-72 overflow-y-auto rounded-md border border-border">
                    {prospects.isLoading ? (
                        <p className="p-3 text-sm text-text-subtle">
                            Loading prospects…
                        </p>
                    ) : prospectItems.length === 0 ? (
                        <p className="p-3 text-sm text-text-subtle">
                            No prospects match.
                        </p>
                    ) : (
                        <ul className="divide-y divide-border">
                            {prospectItems.map((p) => (
                                <li key={p.prospect_id}>
                                    <label className="flex cursor-pointer items-center gap-3 px-3 py-2 text-sm hover:bg-bg-secondary">
                                        <input
                                            type="checkbox"
                                            checked={selectedProspectIds.has(
                                                p.prospect_id,
                                            )}
                                            onChange={() =>
                                                toggleProspect(p.prospect_id)
                                            }
                                        />
                                        <span className="flex-1 truncate">
                                            {p.business_name}
                                        </span>
                                        <span className="text-xs text-text-subtle">
                                            {p.domain ?? "-"}
                                        </span>
                                        <span className="text-xs capitalize text-text-subtle">
                                            {p.pipeline_status ?? "-"}
                                        </span>
                                    </label>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            </Card>

            <div className="sticky bottom-0 -mx-6 border-t border-border bg-bg-main/95 px-6 py-4 backdrop-blur">
                <div className="flex items-center justify-between gap-4">
                    {formErr ? (
                        <p className="text-sm text-accent-rose">{formErr}</p>
                    ) : (
                        <span />
                    )}
                    <button
                        type="button"
                        onClick={handleSubmit}
                        disabled={create.isPending}
                        className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                    >
                        {create.isPending ? "Creating…" : "Create campaign"}
                    </button>
                </div>
            </div>
        </div>
    );
}

function Card({
    title,
    children,
}: {
    title: string;
    children: React.ReactNode;
}) {
    return (
        <section className="space-y-4 rounded-lg border border-border bg-bg-card p-5">
            <h3 className="text-sm font-semibold">{title}</h3>
            {children}
        </section>
    );
}

function Field({
    label,
    help,
    children,
}: {
    label: string;
    help?: string;
    children: React.ReactNode;
}) {
    // Plain <div>, not <label> — the Body field wraps MarkerEditor's
    // toolbar buttons, and a <label> implicitly forwards clicks to the
    // first labelable descendant (a real <button>), which would fire
    // the Bold command any time the surrounding help text was clicked.
    return (
        <div className="block space-y-1">
            <span className="block text-sm font-medium text-text-muted">
                {label}
            </span>
            {children}
            {help ? (
                <span className="block text-xs text-text-subtle">{help}</span>
            ) : null}
        </div>
    );
}
