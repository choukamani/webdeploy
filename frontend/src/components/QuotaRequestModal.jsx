import { useEffect, useState } from "react";
import { Loader2, X, TrendingUp, CheckCircle } from "lucide-react";
import { getMyQuotaUsage, requestQuotaIncrease } from "../services/api";

const FIELD_LABELS = {
  max_deployments_per_day: "Deploiements / jour",
  max_concurrent_deployments: "Deploiements simultanes",
  max_total_deployments: "Total deploiements (-1 = illimite)",
  max_zip_size_mb: "Taille ZIP max (MB)",
};

export default function QuotaRequestModal({ open, onClose, triggerMessage }) {
  const [loading, setLoading] = useState(true);
  const [current, setCurrent] = useState(null);
  const [requested, setRequested] = useState(null);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSuccess(false);
    (async () => {
      try {
        const usage = await getMyQuotaUsage();
        if (cancelled) return;
        setCurrent(usage.quota);
        // Pre-fill requested values with a sensible bump
        setRequested({
          max_deployments_per_day: bump(usage.quota.max_deployments_per_day, 2),
          max_concurrent_deployments: usage.quota.max_concurrent_deployments,
          max_total_deployments: usage.quota.max_total_deployments,
          max_zip_size_mb: usage.quota.max_zip_size_mb,
        });
      } catch {
        if (!cancelled) setError("Impossible de charger vos quotas actuels.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [open]);

  const handleChange = (field, value) => {
    setRequested((prev) => ({ ...prev, [field]: parseInt(value, 10) }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!requested || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await requestQuotaIncrease({
        requested_quota: requested,
        reason: reason.trim(),
      });
      setSuccess(true);
    } catch (err) {
      setError(
        err.response?.data?.detail ||
          "Echec de l'envoi de la demande. Reessayez plus tard."
      );
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-lg">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
          <div className="flex items-center gap-2">
            <TrendingUp className="w-5 h-5 text-indigo-600" />
            <h2 className="font-semibold text-gray-900">
              Demander une augmentation de quota
            </h2>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-md hover:bg-gray-100 cursor-pointer"
          >
            <X className="w-5 h-5 text-gray-500" />
          </button>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-6 h-6 animate-spin text-indigo-600" />
          </div>
        ) : success ? (
          <div className="p-8 text-center">
            <CheckCircle className="w-12 h-12 text-green-500 mx-auto mb-3" />
            <h3 className="font-semibold text-gray-900 mb-1">Demande envoyee</h3>
            <p className="text-sm text-gray-500">
              Les administrateurs ont ete notifies. Vous serez contacte des qu'ils
              auront traite votre demande.
            </p>
            <button
              onClick={onClose}
              className="mt-5 px-5 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium hover:bg-indigo-700 cursor-pointer"
            >
              Fermer
            </button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="p-6 space-y-4">
            {triggerMessage && (
              <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">
                {triggerMessage}
              </div>
            )}

            <p className="text-sm text-gray-600">
              Ajustez les limites ci-dessous et expliquez pourquoi vous en avez besoin.
              Un administrateur recevra un email et une notification dans la plateforme.
            </p>

            <div className="grid grid-cols-2 gap-3">
              {Object.entries(FIELD_LABELS).map(([field, label]) => (
                <div key={field}>
                  <label className="block text-xs font-medium text-gray-500 mb-1">
                    {label}
                  </label>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-gray-400 w-12 text-right">
                      {current?.[field] ?? "-"}
                    </span>
                    <span className="text-xs text-gray-400">&rarr;</span>
                    <input
                      type="number"
                      value={requested?.[field] ?? 0}
                      onChange={(e) => handleChange(field, e.target.value)}
                      className="flex-1 px-2 py-1.5 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                  </div>
                </div>
              ))}
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Motif (optionnel)
              </label>
              <textarea
                rows={3}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Expliquez pourquoi vous avez besoin d'un quota plus eleve..."
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>

            {error && (
              <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
                {error}
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={onClose}
                className="px-4 py-2 text-sm text-gray-700 bg-white border border-gray-200 rounded-lg hover:bg-gray-50 cursor-pointer"
              >
                Annuler
              </button>
              <button
                type="submit"
                disabled={submitting}
                className="flex items-center gap-2 px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium hover:bg-indigo-700 disabled:opacity-50 cursor-pointer"
              >
                {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                Envoyer la demande
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

function bump(value, factor) {
  if (value == null || value < 0) return value;
  return Math.max(value + 1, Math.ceil(value * factor));
}
