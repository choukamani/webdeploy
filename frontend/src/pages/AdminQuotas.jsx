import { useState, useEffect, useCallback } from "react";
import { Loader2, Save, CheckCircle, AlertCircle, Gauge, Users, Inbox, ThumbsUp, ThumbsDown } from "lucide-react";
import {
  getQuotaDefaults, setRoleQuota, listUsers, getUserQuota, setUserQuota,
  listQuotaRequests, approveQuotaRequest, rejectQuotaRequest,
} from "../services/api";

const ROLE_LABELS = {
  simple_user: "Utilisateur Simple",
  super_user: "Super Utilisateur",
  admin: "Administrateur",
};

const FIELD_LABELS = {
  max_deployments_per_day: "Deploiements / jour",
  max_zip_size_mb: "Taille ZIP max (MB)",
  max_concurrent_deployments: "Deploiements simultanes",
  max_total_deployments: "Total deploiements (-1 = illimite)",
};

export default function AdminQuotas() {
  const [roleQuotas, setRoleQuotas] = useState({});
  const [users, setUsers] = useState([]);
  const [selectedUser, setSelectedUser] = useState(null);
  const [userQuota, setUserQuotaState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(null);
  const [message, setMessage] = useState(null);
  const [requests, setRequests] = useState([]);
  const [processingRequest, setProcessingRequest] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const [defaults, userList, reqs] = await Promise.all([
        getQuotaDefaults(),
        listUsers(),
        listQuotaRequests(),
      ]);
      setRoleQuotas(defaults);
      setUsers(userList.filter((u) => u.status === "approved"));
      setRequests(reqs);
    } catch {
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleRoleChange = (role, field, value) => {
    setRoleQuotas((prev) => ({
      ...prev,
      [role]: { ...prev[role], [field]: parseInt(value) || 0 },
    }));
    setMessage(null);
  };

  const handleSaveRole = async (role) => {
    setSaving(role);
    setMessage(null);
    try {
      await setRoleQuota(role, roleQuotas[role]);
      setMessage({ type: "success", text: `Quotas ${ROLE_LABELS[role]} enregistres.` });
    } catch {
      setMessage({ type: "error", text: "Erreur lors de la sauvegarde." });
    } finally {
      setSaving(null);
    }
  };

  const handleSelectUser = async (uid) => {
    setSelectedUser(uid);
    setUserQuotaState(null);
    try {
      const data = await getUserQuota(uid);
      setUserQuotaState(data.config);
    } catch {}
  };

  const handleUserQuotaChange = (field, value) => {
    setUserQuotaState((prev) => ({
      ...prev,
      [field]: parseInt(value) || 0,
    }));
    setMessage(null);
  };

  const handleSaveUserQuota = async () => {
    if (!selectedUser || !userQuota) return;
    setSaving("user");
    setMessage(null);
    try {
      await setUserQuota(selectedUser, userQuota);
      setMessage({ type: "success", text: "Quotas utilisateur enregistres." });
    } catch {
      setMessage({ type: "error", text: "Erreur lors de la sauvegarde." });
    } finally {
      setSaving(null);
    }
  };

  const handleApproveRequest = async (req) => {
    setProcessingRequest(req.id);
    setMessage(null);
    try {
      await approveQuotaRequest(req.id);
      setRequests((prev) =>
        prev.map((r) => (r.id === req.id ? { ...r, status: "approved" } : r))
      );
      setMessage({
        type: "success",
        text: `Demande de ${req.display_name || req.email} approuvee.`,
      });
    } catch {
      setMessage({ type: "error", text: "Impossible d'approuver la demande." });
    } finally {
      setProcessingRequest(null);
    }
  };

  const handleRejectRequest = async (req) => {
    setProcessingRequest(req.id);
    setMessage(null);
    try {
      await rejectQuotaRequest(req.id);
      setRequests((prev) =>
        prev.map((r) => (r.id === req.id ? { ...r, status: "rejected" } : r))
      );
      setMessage({
        type: "success",
        text: `Demande de ${req.display_name || req.email} rejetee.`,
      });
    } catch {
      setMessage({ type: "error", text: "Impossible de rejeter la demande." });
    } finally {
      setProcessingRequest(null);
    }
  };

  const pendingRequests = requests.filter((r) => r.status === "pending");
  const reviewedRequests = requests.filter((r) => r.status !== "pending");

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-8 h-8 animate-spin text-blue-600" />
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Gestion des Quotas</h1>
        <p className="text-sm text-gray-500 mt-1">
          Definir les limites de deploiement par role et par utilisateur
        </p>
      </div>

      {message && (
        <div
          className={`flex items-center gap-2 p-3 rounded-lg text-sm ${
            message.type === "success"
              ? "bg-green-50 text-green-700 border border-green-200"
              : "bg-red-50 text-red-700 border border-red-200"
          }`}
        >
          {message.type === "success" ? <CheckCircle className="w-4 h-4" /> : <AlertCircle className="w-4 h-4" />}
          {message.text}
        </div>
      )}

      {/* Quota increase requests */}
      <div className="space-y-4">
        <h2 className="font-semibold text-gray-900 flex items-center gap-2">
          <Inbox className="w-5 h-5 text-indigo-600" />
          Demandes d'augmentation
          {pendingRequests.length > 0 && (
            <span className="px-2 py-0.5 text-xs font-semibold bg-indigo-100 text-indigo-700 rounded-full">
              {pendingRequests.length}
            </span>
          )}
        </h2>

        {pendingRequests.length === 0 && reviewedRequests.length === 0 ? (
          <div className="bg-white rounded-lg border border-gray-200 p-6 text-sm text-gray-500 text-center">
            Aucune demande pour le moment.
          </div>
        ) : (
          <div className="space-y-3">
            {pendingRequests.map((req) => (
              <div key={req.id} className="bg-white rounded-lg border border-indigo-200 p-5 shadow-sm">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-gray-900">
                        {req.display_name || req.email}
                      </span>
                      <span className="text-xs text-gray-500">{req.email}</span>
                      <span className="text-xs px-2 py-0.5 bg-gray-100 text-gray-700 rounded">
                        {ROLE_LABELS[req.role] || req.role}
                      </span>
                    </div>
                    <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
                      {Object.entries(FIELD_LABELS).map(([field, label]) => {
                        const from = req.current_quota?.[field];
                        const to = req.requested_quota?.[field];
                        const changed = from !== to;
                        return (
                          <div
                            key={field}
                            className={`px-3 py-2 rounded border ${
                              changed ? "border-indigo-200 bg-indigo-50" : "border-gray-100 bg-gray-50"
                            }`}
                          >
                            <div className="text-[10px] uppercase tracking-wide text-gray-500">{label}</div>
                            <div className={`mt-0.5 ${changed ? "text-indigo-700 font-semibold" : "text-gray-700"}`}>
                              {from ?? "-"} &rarr; {to ?? "-"}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                    {req.reason && (
                      <p className="mt-3 text-sm text-gray-600 border-l-4 border-indigo-200 pl-3 bg-indigo-50/40 py-1.5 rounded-r">
                        {req.reason}
                      </p>
                    )}
                  </div>
                  <div className="flex flex-col gap-2 flex-shrink-0">
                    <button
                      onClick={() => handleApproveRequest(req)}
                      disabled={processingRequest === req.id}
                      className="flex items-center gap-1.5 px-3 py-1.5 bg-emerald-600 text-white text-sm rounded-lg font-medium hover:bg-emerald-700 disabled:opacity-50 cursor-pointer"
                    >
                      {processingRequest === req.id ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <ThumbsUp className="w-3.5 h-3.5" />
                      )}
                      Approuver
                    </button>
                    <button
                      onClick={() => handleRejectRequest(req)}
                      disabled={processingRequest === req.id}
                      className="flex items-center gap-1.5 px-3 py-1.5 bg-white border border-red-200 text-red-600 text-sm rounded-lg font-medium hover:bg-red-50 disabled:opacity-50 cursor-pointer"
                    >
                      <ThumbsDown className="w-3.5 h-3.5" />
                      Rejeter
                    </button>
                  </div>
                </div>
              </div>
            ))}

            {reviewedRequests.length > 0 && (
              <details className="bg-white rounded-lg border border-gray-200 p-4">
                <summary className="text-sm text-gray-600 cursor-pointer select-none">
                  Historique ({reviewedRequests.length})
                </summary>
                <div className="mt-3 space-y-2">
                  {reviewedRequests.map((req) => (
                    <div
                      key={req.id}
                      className="flex items-center justify-between text-sm px-3 py-2 bg-gray-50 rounded"
                    >
                      <div>
                        <span className="font-medium text-gray-700">
                          {req.display_name || req.email}
                        </span>
                        <span className="text-xs text-gray-400 ml-2">
                          {new Date(req.created_at).toLocaleString()}
                        </span>
                      </div>
                      <span
                        className={`text-xs px-2 py-0.5 rounded font-medium ${
                          req.status === "approved"
                            ? "bg-green-100 text-green-700"
                            : "bg-gray-200 text-gray-600"
                        }`}
                      >
                        {req.status}
                      </span>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
        )}
      </div>

      {/* Role quotas */}
      <div className="space-y-4">
        <h2 className="font-semibold text-gray-900 flex items-center gap-2">
          <Gauge className="w-5 h-5 text-blue-600" />
          Quotas par role
        </h2>

        {Object.entries(ROLE_LABELS).map(([role, label]) => (
          <div key={role} className="bg-white rounded-lg border border-gray-200 p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-medium text-gray-900">{label}</h3>
              <button
                onClick={() => handleSaveRole(role)}
                disabled={saving === role}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 text-white text-sm rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 cursor-pointer"
              >
                {saving === role ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Save className="w-3.5 h-3.5" />
                )}
                Enregistrer
              </button>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              {Object.entries(FIELD_LABELS).map(([field, fieldLabel]) => (
                <div key={field}>
                  <label className="block text-xs font-medium text-gray-500 mb-1">{fieldLabel}</label>
                  <input
                    type="number"
                    value={roleQuotas[role]?.[field] ?? 0}
                    onChange={(e) => handleRoleChange(role, field, e.target.value)}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* Per-user quotas */}
      <div className="space-y-4">
        <h2 className="font-semibold text-gray-900 flex items-center gap-2">
          <Users className="w-5 h-5 text-purple-600" />
          Quotas par utilisateur (override)
        </h2>

        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-700 mb-1">Selectionner un utilisateur</label>
            <select
              value={selectedUser || ""}
              onChange={(e) => handleSelectUser(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">-- Choisir --</option>
              {users.map((u) => (
                <option key={u.uid} value={u.uid}>
                  {u.display_name || u.email} ({ROLE_LABELS[u.role] || u.role})
                </option>
              ))}
            </select>
          </div>

          {selectedUser && userQuota && (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
                {Object.entries(FIELD_LABELS).map(([field, fieldLabel]) => (
                  <div key={field}>
                    <label className="block text-xs font-medium text-gray-500 mb-1">{fieldLabel}</label>
                    <input
                      type="number"
                      value={userQuota[field] ?? 0}
                      onChange={(e) => handleUserQuotaChange(field, e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                ))}
              </div>
              <button
                onClick={handleSaveUserQuota}
                disabled={saving === "user"}
                className="flex items-center gap-1.5 px-4 py-2 bg-purple-600 text-white text-sm rounded-lg font-medium hover:bg-purple-700 disabled:opacity-50 cursor-pointer"
              >
                {saving === "user" ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Save className="w-3.5 h-3.5" />
                )}
                Enregistrer l'override
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
