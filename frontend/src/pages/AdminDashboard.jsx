import { useState, useEffect, useCallback } from "react";
import {
  Loader2, Activity, Clock, CheckCircle, XCircle, Users,
  AlertTriangle, TrendingUp, Server, RefreshCw
} from "lucide-react";
import { getDashboard, getAlerts, resolveAlert } from "../services/api";

export default function AdminDashboard() {
  const [data, setData] = useState(null);
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const fetchData = useCallback(async (showSpinner = true) => {
    if (showSpinner) setRefreshing(true);
    try {
      const [dashData, alertData] = await Promise.all([
        getDashboard(),
        getAlerts({ resolved: false, limit: 10 }),
      ]);
      setData(dashData);
      setAlerts(alertData);
    } catch (err) {
      console.error("Dashboard fetch failed:", err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchData(false);
    const interval = setInterval(() => fetchData(false), 15000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const handleResolve = async (alertId) => {
    try {
      await resolveAlert(alertId);
      setAlerts((prev) => prev.filter((a) => a.id !== alertId));
    } catch {}
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-8 h-8 animate-spin text-blue-600" />
      </div>
    );
  }

  if (!data) return <p className="text-center text-gray-500 py-10">Impossible de charger le dashboard.</p>;

  const severityColors = {
    info: "bg-blue-50 border-blue-200 text-blue-700",
    warning: "bg-amber-50 border-amber-200 text-amber-700",
    error: "bg-red-50 border-red-200 text-red-700",
    critical: "bg-red-100 border-red-300 text-red-800",
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Dashboard Temps Reel</h1>
          <p className="text-sm text-gray-500">Mise a jour automatique toutes les 15s</p>
        </div>
        <button
          onClick={() => fetchData(true)}
          disabled={refreshing}
          className="flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 rounded-lg text-sm hover:bg-gray-50 cursor-pointer"
        >
          <RefreshCw className={`w-4 h-4 ${refreshing ? "animate-spin" : ""}`} />
          Rafraichir
        </button>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-blue-50 rounded-lg">
              <Activity className="w-5 h-5 text-blue-600" />
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900">{data.active_deployments.length}</p>
              <p className="text-xs text-gray-500">En cours</p>
            </div>
          </div>
        </div>
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-amber-50 rounded-lg">
              <Clock className="w-5 h-5 text-amber-600" />
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900">{data.queue_size}</p>
              <p className="text-xs text-gray-500">En attente</p>
            </div>
          </div>
        </div>
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-green-50 rounded-lg">
              <CheckCircle className="w-5 h-5 text-green-600" />
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900">{data.today.success}</p>
              <p className="text-xs text-gray-500">Succes aujourd'hui</p>
            </div>
          </div>
        </div>
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-red-50 rounded-lg">
              <XCircle className="w-5 h-5 text-red-600" />
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900">{data.today.failed}</p>
              <p className="text-xs text-gray-500">Echecs aujourd'hui</p>
            </div>
          </div>
        </div>
      </div>

      {/* Active deployments */}
      {data.active_deployments.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="font-semibold text-gray-900 mb-4 flex items-center gap-2">
            <Activity className="w-4 h-4 text-blue-600" />
            Deploiements en cours
          </h2>
          <div className="space-y-3">
            {data.active_deployments.map((d) => (
              <div key={d.id} className="flex items-center justify-between p-3 bg-blue-50 rounded-lg">
                <div>
                  <span className="font-medium text-gray-900">{d.website_name}</span>
                  <span className="ml-2 text-xs text-gray-500">{d.mode}</span>
                  <span className="ml-2 text-xs text-gray-400">{d.deployer_email}</span>
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-xs px-2 py-1 bg-blue-100 text-blue-700 rounded font-medium">
                    {d.current_step}
                  </span>
                  <span className="text-xs text-gray-500">{Math.floor(d.elapsed_seconds / 60)}min</span>
                  <div className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Alerts */}
      {alerts.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="font-semibold text-gray-900 mb-4 flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-500" />
            Alertes ({alerts.length})
          </h2>
          <div className="space-y-2">
            {alerts.map((alert) => (
              <div
                key={alert.id}
                className={`flex items-center justify-between p-3 border rounded-lg ${
                  severityColors[alert.severity] || severityColors.info
                }`}
              >
                <div>
                  <span className="font-medium">{alert.title}</span>
                  <p className="text-xs mt-0.5 opacity-80">{alert.message}</p>
                </div>
                <button
                  onClick={() => handleResolve(alert.id)}
                  className="text-xs px-3 py-1 bg-white rounded border hover:bg-gray-50 cursor-pointer"
                >
                  Resoudre
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* 7-day trend */}
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="font-semibold text-gray-900 mb-4 flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-green-600" />
            Tendance 7 jours
          </h2>
          <div className="flex items-end gap-2 h-32">
            {data.trend.map((day) => {
              const max = Math.max(...data.trend.map((d) => d.count), 1);
              const height = (day.count / max) * 100;
              return (
                <div key={day.date} className="flex-1 flex flex-col items-center gap-1">
                  <span className="text-xs font-medium text-gray-700">{day.count}</span>
                  <div
                    className="w-full bg-blue-500 rounded-t"
                    style={{ height: `${Math.max(height, 4)}%` }}
                  />
                  <span className="text-[10px] text-gray-400">
                    {new Date(day.date).toLocaleDateString("fr", { weekday: "short" })}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Services health */}
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="font-semibold text-gray-900 mb-4 flex items-center gap-2">
            <Server className="w-4 h-4 text-purple-600" />
            Sante des services
          </h2>
          <div className="space-y-3">
            {data.services.map((svc) => (
              <div key={svc.name} className="flex items-center justify-between">
                <span className="text-sm text-gray-700">{svc.name}</span>
                <div className="flex items-center gap-2">
                  {svc.message && (
                    <span className="text-xs text-gray-400">{svc.message}</span>
                  )}
                  <div
                    className={`w-3 h-3 rounded-full ${
                      svc.status === "healthy"
                        ? "bg-green-500"
                        : svc.status === "warning"
                        ? "bg-amber-500"
                        : "bg-red-500"
                    }`}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Users + Recent deployments */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="font-semibold text-gray-900 mb-4 flex items-center gap-2">
            <Users className="w-4 h-4 text-indigo-600" />
            Utilisateurs
          </h2>
          <div className="space-y-2">
            <div className="flex justify-between">
              <span className="text-sm text-gray-500">Total</span>
              <span className="font-semibold">{data.users.total}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-sm text-gray-500">Approuves</span>
              <span className="font-semibold text-green-600">{data.users.approved}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-sm text-gray-500">En attente</span>
              <span className="font-semibold text-amber-600">{data.users.pending}</span>
            </div>
          </div>
        </div>

        <div className="col-span-2 bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="font-semibold text-gray-900 mb-4">Derniers deploiements</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b">
                  <th className="pb-2">Site</th>
                  <th className="pb-2">Mode</th>
                  <th className="pb-2">Statut</th>
                  <th className="pb-2">Deployer</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_deployments.map((d) => (
                  <tr key={d.id} className="border-b border-gray-50">
                    <td className="py-2 font-medium">{d.website_name}</td>
                    <td className="py-2">
                      <span className="text-xs px-2 py-0.5 bg-gray-100 rounded">{d.mode}</span>
                    </td>
                    <td className="py-2">
                      <span
                        className={`text-xs px-2 py-0.5 rounded font-medium ${
                          d.status === "success"
                            ? "bg-green-100 text-green-700"
                            : d.status === "failed"
                            ? "bg-red-100 text-red-700"
                            : d.status === "running"
                            ? "bg-blue-100 text-blue-700"
                            : "bg-gray-100 text-gray-700"
                        }`}
                      >
                        {d.status}
                      </span>
                    </td>
                    <td className="py-2 text-gray-500">{d.deployer_email}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
