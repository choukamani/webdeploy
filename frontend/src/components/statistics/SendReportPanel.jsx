import { useState } from "react";
import { Send, Loader, CheckCircle, X } from "lucide-react";
import { sendOnDemandReport } from "../../services/api";

export default function SendReportPanel({ startDate, endDate, onClose }) {
  const [sendToDeployers, setSendToDeployers] = useState(true);
  const [sendToAdmins, setSendToAdmins] = useState(true);
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const handleSend = async () => {
    setSending(true);
    setError(null);
    setResult(null);
    try {
      const res = await sendOnDemandReport({
        start_date: startDate,
        end_date: endDate,
        send_to_deployers: sendToDeployers,
        send_to_admins: sendToAdmins,
      });
      setResult(res);
    } catch (err) {
      setError(err.response?.data?.detail || err.message || "Erreur lors de l'envoi");
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
          <h3 className="text-lg font-semibold text-gray-900">Envoyer un rapport</h3>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-6 py-5 space-y-4">
          <div className="text-sm text-gray-600">
            Période : <span className="font-medium text-gray-900">{startDate}</span> au{" "}
            <span className="font-medium text-gray-900">{endDate}</span>
          </div>

          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={sendToDeployers}
              onChange={(e) => setSendToDeployers(e.target.checked)}
              className="w-4 h-4 rounded border-gray-300 text-[#2563EB] focus:ring-[#2563EB]"
            />
            <span className="text-sm text-gray-700">
              Envoyer un rapport personnalisé à chaque déployeur
            </span>
          </label>

          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={sendToAdmins}
              onChange={(e) => setSendToAdmins(e.target.checked)}
              className="w-4 h-4 rounded border-gray-300 text-[#2563EB] focus:ring-[#2563EB]"
            />
            <span className="text-sm text-gray-700">
              Envoyer le résumé complet aux admins
            </span>
          </label>

          {error && (
            <div className="text-sm text-red-600 bg-red-50 rounded-lg px-4 py-2">
              {error}
            </div>
          )}

          {result && (
            <div className="flex items-start gap-2 text-sm text-green-700 bg-green-50 rounded-lg px-4 py-3">
              <CheckCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
              <div>
                <p className="font-medium">
                  {result.emails_sent} email{result.emails_sent !== 1 ? "s" : ""} envoyé
                  {result.emails_sent !== 1 ? "s" : ""}
                </p>
                <p className="text-green-600 mt-1">
                  {result.recipients?.join(", ")}
                </p>
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-200 bg-gray-50 rounded-b-xl">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-gray-700 hover:text-gray-900 transition-colors"
          >
            Fermer
          </button>
          {!result && (
            <button
              onClick={handleSend}
              disabled={sending || (!sendToDeployers && !sendToAdmins)}
              className="inline-flex items-center gap-2 px-4 py-2 bg-[#2563EB] text-white
                rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors
                disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {sending ? (
                <Loader className="w-4 h-4 animate-spin" />
              ) : (
                <Send className="w-4 h-4" />
              )}
              Envoyer
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
