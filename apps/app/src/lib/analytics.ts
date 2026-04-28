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

export type AnalyticsPeriod = "7d" | "30d" | "90d";

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
