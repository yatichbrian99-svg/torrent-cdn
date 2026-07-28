'use client';

import { useState, useEffect } from 'react';
import { BarChart3, TrendingUp, HardDrive, Users, Activity } from 'lucide-react';
import api from '@/lib/api';

interface AnalyticsData {
  total_views: number;
  p2p_offload_percent: number;
  bandwidth_saved_gb: number;
  active_peers: number;
}

interface AnalyticsProps {
  fileId: string;
  className?: string;
}

export default function Analytics({ fileId, className = '' }: AnalyticsProps) {
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadAnalytics();
    const interval = setInterval(loadAnalytics, 10000); // Refresh every 10s
    return () => clearInterval(interval);
  }, [fileId]);

  const loadAnalytics = async () => {
    try {
      const res = await api.getAnalytics(fileId);
      setData(res.data);
    } catch {
      // Silently fail
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className={`animate-pulse rounded-xl bg-slate-800/30 p-4 ${className}`}>
        <div className="h-4 w-24 rounded bg-slate-700" />
        <div className="mt-3 grid grid-cols-2 gap-3">
          <div className="h-16 rounded bg-slate-700/50" />
          <div className="h-16 rounded bg-slate-700/50" />
          <div className="h-16 rounded bg-slate-700/50" />
          <div className="h-16 rounded bg-slate-700/50" />
        </div>
      </div>
    );
  }

  if (!data) return null;

  const stats = [
    {
      label: 'Total Views',
      value: data.total_views.toLocaleString(),
      icon: BarChart3,
      color: 'text-blue-400',
      bg: 'bg-blue-500/10',
    },
    {
      label: 'P2P Offload',
      value: `${data.p2p_offload_percent.toFixed(1)}%`,
      icon: TrendingUp,
      color: 'text-green-400',
      bg: 'bg-green-500/10',
    },
    {
      label: 'Bandwidth Saved',
      value: `${data.bandwidth_saved_gb.toFixed(3)} GB`,
      icon: HardDrive,
      color: 'text-purple-400',
      bg: 'bg-purple-500/10',
    },
    {
      label: 'Active Peers',
      value: data.active_peers.toString(),
      icon: Users,
      color: 'text-orange-400',
      bg: 'bg-orange-500/10',
    },
  ];

  return (
    <div className={`rounded-xl border border-slate-700/50 bg-slate-800/30 p-4 backdrop-blur-xl ${className}`}>
      <div className="flex items-center gap-2">
        <Activity className="h-4 w-4 text-primary-400" />
        <h3 className="text-sm font-medium text-white">Real-time Analytics</h3>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3">
        {stats.map((stat) => (
          <div key={stat.label} className="rounded-lg bg-slate-900/50 p-3">
            <div className="flex items-center gap-1.5">
              <div className={`flex h-6 w-6 items-center justify-center rounded-md ${stat.bg}`}>
                <stat.icon className={`h-3.5 w-3.5 ${stat.color}`} />
              </div>
              <span className="text-xs text-slate-400">{stat.label}</span>
            </div>
            <p className="mt-1 text-lg font-bold text-white">{stat.value}</p>
          </div>
        ))}
      </div>

      {/* Offload progress bar */}
      <div className="mt-3">
        <div className="flex items-center justify-between text-xs">
          <span className="text-slate-400">P2P Efficiency</span>
          <span className="text-primary-400">{data.p2p_offload_percent.toFixed(1)}%</span>
        </div>
        <div className="mt-1 h-2 overflow-hidden rounded-full bg-slate-700">
          <div
            className="h-full rounded-full bg-gradient-to-r from-primary-500 to-green-500 transition-all"
            style={{ width: `${Math.min(data.p2p_offload_percent, 100)}%` }}
          />
        </div>
      </div>
    </div>
  );
}
