"use client";

import { useState } from "react";
import { Modal } from "@/components/admin/modal";
import { useCreateUser } from "@/lib/users";
import type { UserRole } from "@/lib/me";

/**
 * Lightweight "Invite teammate" dialog for the dashboard quick-action.
 *
 * The platform doesn't have magic-link email invites yet (the /users
 * endpoint creates a user with a password the admin must hand off
 * out-of-band). This dialog mirrors that flow: admin enters email +
 * name + role, we generate a temp password, and surface it on success
 * so the admin can paste it into Slack / email manually.
 *
 * For deeper team management (deactivate, role changes, seat
 * reconciliation), the /team page remains the source of truth — the
 * footer links there.
 */
export function InviteTeammatesDialog({
    open,
    onClose,
}: {
    open: boolean;
    onClose: () => void;
}) {
    const [email, setEmail] = useState("");
    const [name, setName] = useState("");
    const [role, setRole] = useState<UserRole>("agent");
    const [tempPassword, setTempPassword] = useState("");
    const [created, setCreated] = useState<{
        email: string;
        password: string;
    } | null>(null);
    const [err, setErr] = useState<string | null>(null);

    const createMut = useCreateUser();

    const reset = () => {
        setEmail("");
        setName("");
        setRole("agent");
        setTempPassword("");
        setCreated(null);
        setErr(null);
    };

    const close = () => {
        reset();
        onClose();
    };

    const generate = () => {
        // Memorable enough to type once, random enough not to collide.
        const rand = Math.random().toString(36).slice(2, 8);
        setTempPassword(`linda-${rand}`);
    };

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        setErr(null);
        if (!email.trim() || !tempPassword || tempPassword.length < 8) {
            setErr("Email and an 8+ character temporary password are required.");
            return;
        }
        try {
            await createMut.mutateAsync({
                email: email.trim().toLowerCase(),
                name: name.trim() || undefined,
                role,
                password: tempPassword,
            });
            setCreated({ email: email.trim().toLowerCase(), password: tempPassword });
        } catch (e: unknown) {
            const m = e instanceof Error ? e.message : "Invite failed";
            setErr(m);
        }
    };

    return (
        <Modal open={open} onClose={close} title="Invite teammate">
            {created ? (
                <div className="space-y-3 text-sm">
                    <p>
                        Invited <strong>{created.email}</strong>. We don&apos;t
                        send email invites yet — share these credentials with
                        them out-of-band:
                    </p>
                    <div className="rounded-md border border-border bg-bg-secondary p-3 font-mono text-xs">
                        <div>
                            <span className="text-text-subtle">Email:</span>{" "}
                            {created.email}
                        </div>
                        <div className="mt-1">
                            <span className="text-text-subtle">Password:</span>{" "}
                            {created.password}
                        </div>
                    </div>
                    <p className="text-text-muted">
                        They can sign in at{" "}
                        <span className="font-mono">/sign-in</span>. Ask them to
                        change the password after their first login.
                    </p>
                    <div className="flex justify-end gap-2 pt-2">
                        <button
                            type="button"
                            onClick={reset}
                            className="btn btn-ghost text-sm"
                        >
                            Invite another
                        </button>
                        <button
                            type="button"
                            onClick={close}
                            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-white"
                        >
                            Done
                        </button>
                    </div>
                </div>
            ) : (
                <form onSubmit={submit} className="space-y-3">
                    <div>
                        <label className="text-xs font-medium uppercase tracking-wide text-text-subtle">
                            Email
                        </label>
                        <input
                            type="email"
                            required
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            placeholder="name@company.com"
                            className="mt-1 w-full rounded-md border border-border bg-bg-card px-3 py-2 text-sm"
                        />
                    </div>
                    <div>
                        <label className="text-xs font-medium uppercase tracking-wide text-text-subtle">
                            Name (optional)
                        </label>
                        <input
                            type="text"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            placeholder="Full name"
                            className="mt-1 w-full rounded-md border border-border bg-bg-card px-3 py-2 text-sm"
                        />
                    </div>
                    <div>
                        <label className="text-xs font-medium uppercase tracking-wide text-text-subtle">
                            Role
                        </label>
                        <select
                            value={role}
                            onChange={(e) => setRole(e.target.value as UserRole)}
                            className="mt-1 w-full rounded-md border border-border bg-bg-card px-3 py-2 text-sm"
                        >
                            <option value="agent">Agent</option>
                            <option value="manager">Manager</option>
                            <option value="admin">Admin</option>
                        </select>
                    </div>
                    <div>
                        <label className="text-xs font-medium uppercase tracking-wide text-text-subtle">
                            Temporary password
                        </label>
                        <div className="mt-1 flex gap-2">
                            <input
                                type="text"
                                required
                                minLength={8}
                                value={tempPassword}
                                onChange={(e) => setTempPassword(e.target.value)}
                                placeholder="Generate or type one (8+ chars)"
                                className="flex-1 rounded-md border border-border bg-bg-card px-3 py-2 font-mono text-sm"
                            />
                            <button
                                type="button"
                                onClick={generate}
                                className="rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm hover:bg-bg-card-hover"
                            >
                                Generate
                            </button>
                        </div>
                        <p className="mt-1 text-xs text-text-subtle">
                            We&apos;ll show this on the next screen so you can
                            send it to them.
                        </p>
                    </div>
                    {err ? (
                        <p className="rounded-md border border-accent-rose/40 bg-accent-rose/10 px-3 py-2 text-xs text-accent-rose">
                            {err}
                        </p>
                    ) : null}
                    <div className="flex items-center justify-between gap-2 pt-2 text-xs text-text-subtle">
                        <span>
                            Need to deactivate or change roles? Visit the{" "}
                            <a href="/team" className="text-primary hover:underline">
                                Team page
                            </a>
                            .
                        </span>
                        <div className="flex gap-2">
                            <button
                                type="button"
                                onClick={close}
                                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-card-hover"
                            >
                                Cancel
                            </button>
                            <button
                                type="submit"
                                disabled={createMut.isPending}
                                className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                            >
                                {createMut.isPending ? "Inviting…" : "Invite"}
                            </button>
                        </div>
                    </div>
                </form>
            )}
        </Modal>
    );
}
