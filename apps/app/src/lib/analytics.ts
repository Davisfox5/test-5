"use client";

import { useQuery } from "@tanstack/react-query";
import { useApi } from "./api";

export interface ChannelBreakdown {
    channel: string;
    count: number;
    avg_sentiment: number | null;
}

export interface TopTopic {
    name: string;
    mentions: number;
    avg_relevance: number | null;
}

export interface BusinessHealth {
    health_score: number;
    total_interactions: number;
    avg_sentiment: number | null;
    channels_breakdown: ChannelBreakdown[];
    top_topics: TopTopic[];
}

export interface AgentStats {
    agent_id: string;
    name: string | null;
    interaction_count: number;
    avg_sentiment: number | null;
    avg_scorecard_score: number | null;
    churn_flags: number;
}

export interface TopicTrend {
    name: string;
    mentions: number;
    avg_relevance: number | null;
    pct_change: number | null;
}

export interface CoachingInsights {
    avg_script_adherence: number | null;
    top_compliance_gaps: Array<{ text: string; count: number }>;
    top_improvements: Array<{ text: string; count: number }>;
    top_strengths: Array<{ text: string; count: number }>;
}

export type AnalyticsPeriod = "7d" | "14d" | "30d" | "60d" | "90d";

export function useBusinessHealth(period: AnalyticsPeriod = "30d") {
    const api = useApi();
    return useQuery({
        queryKey: ["analytics", "business", period],
        queryFn: () =>
            api.get<BusinessHealth>(`/analytics/business?period=${period}`),
    });
}

export function useTeamStats() {
    const api = useApi();
    return useQuery({
        queryKey: ["analytics", "team"],
        queryFn: () => api.get<AgentStats[]>("/analytics/team"),
    });
}

export function useTopicsTrend(period: AnalyticsPeriod = "30d") {
    const api = useApi();
    return useQuery({
        queryKey: ["analytics", "topics", period],
        queryFn: () =>
            api.get<TopicTrend[]>(`/analytics/topics?period=${period}`),
    });
}

export function useCoachingInsights(period: AnalyticsPeriod = "30d") {
    const api = useApi();
    return useQuery({
        queryKey: ["analytics", "coaching", period],
        queryFn: () =>
            api.get<CoachingInsights>(`/analytics/coaching?period=${period}`),
    });
}

export interface TrendPoint {
    date: string;
    channel: string | null;
    interaction_count: number;
    avg_sentiment: number | null;
    avg_qa_score?: number | null;
    avg_rapport?: number | null;
}

export function useTrends(period: AnalyticsPeriod = "30d") {
    const api = useApi();
    return useQuery({
        queryKey: ["analytics", "trends", period],
        queryFn: () => api.get<TrendPoint[]>(`/analytics/trends?period=${period}`),
    });
}

export interface SignalBuckets {
    churn: { high: number; medium: number; low: number; none: number };
    upsell: { high: number; medium: number; low: number; none: number };
    avg_churn_risk: number | null;
    avg_upsell_score: number | null;
    by_channel: Array<{
        channel: string;
        churn_flags: number;
        upsell_flags: number;
        total: number;
    }>;
}

export function useSignals(period: AnalyticsPeriod = "30d") {
    const api = useApi();
    return useQuery({
        queryKey: ["analytics", "signals", period],
        queryFn: () => api.get<SignalBuckets>(`/analytics/signals?period=${period}`),
    });
}

export interface AccountHealthRow {
    customer_id: string;
    name: string;
    score: number | null;
    last_touch_at: string | null;
}

export interface AccountHealth {
    at_risk: AccountHealthRow[];
    upsell: AccountHealthRow[];
    stale: AccountHealthRow[];
}

export function useAccountHealth(staleDays = 30, limit = 5) {
    const api = useApi();
    return useQuery({
        queryKey: ["analytics", "account-health", staleDays, limit],
        queryFn: () =>
            api.get<AccountHealth>(
                `/analytics/account-health?stale_days=${staleDays}&limit=${limit}`,
            ),
        // Stale-account view depends on tables that exist on every
        // tenant — but the customer FKs may not be populated on a fresh
        // sandbox; let the query fail quietly so the dashboard still loads.
        retry: false,
    });
}

// Methodology + talk-listen are manager-only on the backend. We let the
// query fail open so an agent's dashboard still renders without 403
// noise; the consumer reads ``error`` to hide its panel.
export interface RepTalkListenRow {
    rep_id: string | null;
    rep_name: string | null;
    call_count: number;
    talk_pct_avg: number | null;
}

export interface MethodologyAdherenceRow {
    framework: string;
    total_calls: number;
    avg_coverage_ratio: number | null;
    most_missed_stage: string | null;
}

export interface ManagerDashboardOverview {
    window_days: number;
    talk_listen: {
        rows: RepTalkListenRow[];
        tenant_avg_talk_pct: number | null;
    };
    churn_throughput: {
        window_days: number;
        buckets: Array<{ bucket: string; count: number }>;
        total_calls: number;
    };
    methodology: MethodologyAdherenceRow[];
}

export function useManagerOverview(windowDays = 30) {
    const api = useApi();
    return useQuery({
        queryKey: ["manager-dashboard-overview", windowDays],
        queryFn: () =>
            api.get<ManagerDashboardOverview>(
                `/manager/dashboard/overview?window_days=${windowDays}`,
            ),
        retry: false,
    });
}
