import { useEffect, useState, useCallback } from "react";
import {
  Loader,
  RefreshCw,
  Send,
  TrendingUp,
  Bot,
  BotOff,
  Users,
  Calendar,
} from "lucide-react";
import { getStatistics } from "../services/api";
import DailyBarChart from "../components/statistics/DailyBarChart";
import StatusPieChart from "../components/statistics/StatusPieChart";
import DeployerTable from "../components/statistics/DeployerTable";
import SendReportPanel from "../components/statistics/SendReportPanel";

const PRESETS = [
  { key: "today", label: "Aujourd'hui" },
  { key: "3days", label: "3 jours" },
  { key: "7days", label: "7 jours" },
  { key: "30days", label: "30 jours" },
  { key: "custom", label: "Personnalisé" },
];

function StatCard({ icon: Icon, label, value, color = "text-gray-900" }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 px-5 py-4">
      <div className="flex items-center gap-2 mb-1">
        <Icon className="w-4 h-4 text-gray-400" />
        <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">
          {label}
        </span>
      </div>
      <p className={`text-2xl font-bold ${color}`}>{value}</p>
    </div>
  );
}

export default function Statistics() {
  const [preset, setPreset] = useState("7days");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reportOpen, setReportOpen] = useState(false);

  const fetchStats = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      let params;
      if (preset === "custom") {
        if (!customStart || !customEnd) return;
        params = { start_date: customStart, end_date: customEnd };
      } else {
        params = { preset };
      }
      const data = await getStatistics(params);
      setStats(data);
    } catch (err) {
      setError(err.response?.data?.detail || err.message || "Erreur de chargement");
    } finally {
      setLoading(false);
    }
  }, [preset, customStart, customEnd]);

  useEffect(() => {
    if (preset === "custom" && (!customStart || !customEnd)) return;
    fetchStats();
  }, [fetchStats]);

  const totalDeployers = stats?.per_deployer?.length || 0;

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Statistiques Pipelines</h1>
          <p className="mt-1 text-gray-600">
            Activité de déploiement et répartition IA
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchStats}
            className="inline-flex items-center gap-2 px-4 py-2 bg-white border border-gray-200
              rounded-lg text-sm font-medium text-gray-700 hover:bg-gray-50 transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Actualiser
          </button>
          <button
            onClick={() => setReportOpen(true)}
            disabled={!stats}
            className="inline-flex items-center gap-2 px-4 py-2 bg-[#2563EB] text-white
              rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors
              disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Send className="w-4 h-4" />
            Envoyer Rapport
          </button>
        </div>
      </div>

      {/* Preset buttons */}
      <div className="flex flex-wrap items-center gap-2 mb-6">
        {PRESETS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setPreset(key)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              preset === key
                ? "bg-[#2563EB] text-white"
                : "bg-white border border-gray-200 text-gray-700 hover:bg-gray-50"
            }`}
          >
            {label}
          </button>
        ))}

        {preset === "custom" && (
          <div className="flex items-center gap-2 ml-2">
            <input
              type="date"
              value={customStart}
              onChange={(e) => setCustomStart(e.target.value)}
              className="px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none
                focus:ring-2 focus:ring-[#2563EB] focus:border-transparent"
            />
            <span className="text-gray-400 text-sm">au</span>
            <input
              type="date"
              value={customEnd}
              onChange={(e) => setCustomEnd(e.target.value)}
              className="px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none
                focus:ring-2 focus:ring-[#2563EB] focus:border-transparent"
            />
          </div>
        )}
      </div>

      {/* Loading */}
      {loading && (
        <div className="flex items-center justify-center py-24">
          <Loader className="w-8 h-8 text-[#2563EB] animate-spin" />
        </div>
      )}

      {/* Error */}
      {error && !loading && (
        <div className="text-center py-16">
          <p className="text-red-600 font-medium mb-4">{error}</p>
          <button
            onClick={fetchStats}
            className="inline-flex items-center gap-2 px-4 py-2 bg-[#2563EB] text-white
              rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Réessayer
          </button>
        </div>
      )}

      {/* Stats */}
      {stats && !loading && (
        <div className="space-y-6">
          {/* Summary cards */}
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
            <StatCard
              icon={TrendingUp}
              label="Total"
              value={stats.total_count}
            />
            <StatCard
              icon={Bot}
              label="Avec IA"
              value={stats.with_ai_count}
              color="text-[#2563EB]"
            />
            <StatCard
              icon={BotOff}
              label="Sans IA"
              value={stats.without_ai_count}
              color="text-gray-600"
            />
            <StatCard
              icon={Calendar}
              label="Moy / Jour"
              value={stats.average_per_day}
            />
            <StatCard
              icon={Users}
              label="Déployeurs"
              value={totalDeployers}
            />
          </div>

          {/* Charts row */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2 bg-white rounded-xl border border-gray-200 p-6">
              <h2 className="text-lg font-semibold text-gray-900 mb-4">
                Répartition journalière
              </h2>
              <DailyBarChart data={stats.daily_breakdown} />
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-6">
              <h2 className="text-lg font-semibold text-gray-900 mb-4">
                Statut des pipelines
              </h2>
              <StatusPieChart data={stats.per_status} />
            </div>
          </div>

          {/* Deployer table */}
          <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-200">
              <h2 className="text-lg font-semibold text-gray-900">
                Activité par déployeur
              </h2>
            </div>
            <DeployerTable deployers={stats.per_deployer} />
          </div>

          {/* AI Usage footer */}
          {stats.ai_tokens && (stats.ai_tokens.input_tokens > 0 || stats.ai_tokens.output_tokens > 0) && (
            <div className="bg-blue-50 border border-blue-200 rounded-xl px-6 py-4">
              <h3 className="text-sm font-semibold text-blue-900 mb-1">
                Utilisation IA
              </h3>
              <p className="text-sm text-blue-700">
                {(stats.ai_tokens.input_tokens / 1000).toFixed(1)}K tokens entrée +{" "}
                {(stats.ai_tokens.output_tokens / 1000).toFixed(1)}K tokens sortie ={" "}
                <span className="font-semibold">
                  ${stats.ai_tokens.estimated_cost_usd.toFixed(4)}
                </span>
              </p>
            </div>
          )}
        </div>
      )}

      {/* Report modal */}
      {reportOpen && stats && (
        <SendReportPanel
          startDate={stats.period_start}
          endDate={stats.period_end}
          onClose={() => setReportOpen(false)}
        />
      )}
    </div>
  );
}
