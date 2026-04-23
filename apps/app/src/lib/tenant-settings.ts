"use client";

/**
 * Hook for reading + writing tenant-level settings.
 *
 * Backed by GET/PATCH /admin/tenant-settings. The PATCH merges into
 * features_enabled key-by-key on the server, so callers can flip a
 * single flag without shipping the whole feature map each time.
 *
 * The feature_flag_spec returned alongside the settings drives the
 * UI — label, help text, and default for each flag come from the
 * backend so adding a new toggle is a one-file change there.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export interface FeatureFlagSpec {
    key: string;
    default: boolean;
    label: string;
    help: string;
}

export interface TenantSettings {
    tenant_id: string;
    transcription_engine: "deepgram" | "whisper";
    automation_level: "approval" | "auto" | "shadow";
    pii_redaction_enabled: boolean;
    translation_enabled: boolean;
    default_language: string;
    keyterm_boost_list: string[];
    question_keyterms: string[];
    features_enabled: Record<string, boolean>;
    feature_flag_spec: FeatureFlagSpec[];
    plan_tier?: string;
    seat_limit?: number;
    admin_seat_limit?: number;
}

export interface TenantSettingsPatch {
    transcription_engine?: TenantSettings["transcription_engine"];
    automation_level?: TenantSettings["automation_level"];
    pii_redaction_enabled?: boolean;
    translation_enabled?: boolean;
    default_language?: string;
    keyterm_boost_list?: string[];
    question_keyterms?: string[];
    features_enabled?: Record<string, boolean>;
}

export function useTenantSettings() {
    const api = useApi();
    return useQuery({
        queryKey: ["tenant-settings"],
        queryFn: () => api.get<TenantSettings>("/admin/tenant-settings"),
        // Settings rarely change; keep the cached value fresh for a
        // minute so toggling one flag doesn't refetch everything.
        staleTime: 60_000,
    });
}

export function useUpdateTenantSettings() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (patch: TenantSettingsPatch) =>
            api.patch<TenantSettings>("/admin/tenant-settings", patch),
        // Optimistic update: flip the local copy immediately so the
        // toggle animation feels snappy, roll back if the PATCH fails.
        onMutate: async (patch) => {
            await qc.cancelQueries({ queryKey: ["tenant-settings"] });
            const previous = qc.getQueryData<TenantSettings>(["tenant-settings"]);
            if (previous && patch.features_enabled) {
                qc.setQueryData<TenantSettings>(["tenant-settings"], {
                    ...previous,
                    features_enabled: {
                        ...previous.features_enabled,
                        ...patch.features_enabled,
                    },
                });
            }
            return { previous };
        },
        onError: (_err, _patch, context) => {
            if (context?.previous) {
                qc.setQueryData(["tenant-settings"], context.previous);
            }
        },
        onSettled: () => {
            qc.invalidateQueries({ queryKey: ["tenant-settings"] });
        },
    });
}
