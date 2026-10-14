import { useState, useEffect, useCallback } from "react";
import { Bell, Mail, FileText, Save, Loader2, CheckCircle, AlertCircle } from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { getNotificationPreferences, updateNotificationPreferences } from "../services/api";

export default function Settings() {
  const { userProfile, isAdmin } = useAuth();
  const [prefs, setPrefs] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  const fetchPrefs = useCallback(async () => {
    try {
      const data = await getNotificationPreferences();
      setPrefs(data);
    } catch {
      setPrefs({
        deployment_notifications: true,
        account_notifications: true,
        report_enabled: false,
        report_frequency: "daily",
        report_custom_start: null,
        report_custom_end: null,
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchPrefs();
  }, [fetchPrefs]);

  const handleToggle = (key) => {
    setPrefs((prev) => ({ ...prev, [key]: !prev[key] }));
    setMessage(null);
  };

  const handleChange = (key, value) => {
    setPrefs((prev) => ({ ...prev, [key]: value }));
    setMessage(null);
  };

  const handleSave = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const updated = await updateNotificationPreferences(prefs);
      setPrefs(updated);
      setMessage({ type: "success", text: "Preferences enregistrees avec succes." });
    } catch {
      setMessage({ type: "error", text: "Erreur lors de la sauvegarde." });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-8 h-8 animate-spin text-blue-600" />
      </div>
    );
  }

  const roleLabel = {
    admin: "Administrateur",
    super_user: "Super Utilisateur",
    simple_user: "Utilisateur Simple",
  };

  return (
    <div className="max-w-2xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Parametres de Notifications</h1>
        <p className="text-sm text-gray-500 mt-1">
          {userProfile?.display_name || userProfile?.email} — {roleLabel[userProfile?.role] || userProfile?.role}
        </p>
      </div>

      <div className="space-y-6">
        {/* Deployment Notifications */}
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <div className="flex items-start gap-4">
            <div className="p-2 bg-blue-50 rounded-lg">
              <Bell className="w-5 h-5 text-blue-600" />
            </div>
            <div className="flex-1">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="font-semibold text-gray-900">Notifications de deploiement</h3>
                  <p className="text-sm text-gray-500 mt-0.5">
                    Recevoir un email a chaque deploiement (succes ou echec)
                  </p>
                </div>
                <button
                  onClick={() => handleToggle("deployment_notifications")}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors cursor-pointer ${
                    prefs.deployment_notifications ? "bg-blue-600" : "bg-gray-300"
                  }`}
                >
                  <span
                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      prefs.deployment_notifications ? "translate-x-6" : "translate-x-1"
                    }`}
                  />
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Account Notifications */}
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <div className="flex items-start gap-4">
            <div className="p-2 bg-green-50 rounded-lg">
              <Mail className="w-5 h-5 text-green-600" />
            </div>
            <div className="flex-1">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="font-semibold text-gray-900">Notifications de compte</h3>
                  <p className="text-sm text-gray-500 mt-0.5">
                    Recevoir un email lors de l'approbation ou du rejet de votre compte
                  </p>
                </div>
                <button
                  onClick={() => handleToggle("account_notifications")}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors cursor-pointer ${
                    prefs.account_notifications ? "bg-blue-600" : "bg-gray-300"
                  }`}
                >
                  <span
                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      prefs.account_notifications ? "translate-x-6" : "translate-x-1"
                    }`}
                  />
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Reports */}
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <div className="flex items-start gap-4">
            <div className="p-2 bg-purple-50 rounded-lg">
              <FileText className="w-5 h-5 text-purple-600" />
            </div>
            <div className="flex-1">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h3 className="font-semibold text-gray-900">Rapports par email</h3>
                  <p className="text-sm text-gray-500 mt-0.5">
                    {isAdmin
                      ? "Recevoir des rapports de statistiques de deploiement"
                      : "Recevoir des rapports personnalises de vos deploiements"}
                  </p>
                </div>
                <button
                  onClick={() => handleToggle("report_enabled")}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors cursor-pointer ${
                    prefs.report_enabled ? "bg-blue-600" : "bg-gray-300"
                  }`}
                >
                  <span
                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      prefs.report_enabled ? "translate-x-6" : "translate-x-1"
                    }`}
                  />
                </button>
              </div>

              {prefs.report_enabled && (
                <div className="mt-4 pt-4 border-t border-gray-100 space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-2">
                      Frequence des rapports
                    </label>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                      {[
                        { value: "daily", label: "Quotidien" },
                        { value: "weekly", label: "Hebdomadaire" },
                        { value: "monthly", label: "Mensuel" },
                        { value: "custom", label: "Personnalise" },
                      ].map((opt) => (
                        <button
                          key={opt.value}
                          onClick={() => handleChange("report_frequency", opt.value)}
                          className={`px-3 py-2 rounded-md text-sm font-medium border transition-colors cursor-pointer ${
                            prefs.report_frequency === opt.value
                              ? "bg-blue-600 text-white border-blue-600"
                              : "bg-white text-gray-700 border-gray-300 hover:bg-gray-50"
                          }`}
                        >
                          {opt.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {prefs.report_frequency === "custom" && (
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <label className="block text-sm font-medium text-gray-700 mb-1">
                          Date de debut
                        </label>
                        <input
                          type="date"
                          value={prefs.report_custom_start || ""}
                          onChange={(e) => handleChange("report_custom_start", e.target.value)}
                          className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                        />
                      </div>
                      <div>
                        <label className="block text-sm font-medium text-gray-700 mb-1">
                          Date de fin
                        </label>
                        <input
                          type="date"
                          value={prefs.report_custom_end || ""}
                          onChange={(e) => handleChange("report_custom_end", e.target.value)}
                          className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                        />
                      </div>
                    </div>
                  )}

                  <div className="bg-gray-50 rounded-md p-3">
                    <p className="text-xs text-gray-500">
                      {prefs.report_frequency === "daily" &&
                        "Vous recevrez un rapport chaque jour ouvrable a 18h00 (heure de Paris)."}
                      {prefs.report_frequency === "weekly" &&
                        "Vous recevrez un rapport chaque vendredi a 18h00 (heure de Paris)."}
                      {prefs.report_frequency === "monthly" &&
                        "Vous recevrez un rapport le 1er de chaque mois a 09h00 (heure de Paris)."}
                      {prefs.report_frequency === "custom" &&
                        "Vous recevrez un rapport couvrant la periode personnalisee que vous avez definie."}
                    </p>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Save button */}
        <div className="flex items-center justify-between">
          <div>
            {message && (
              <div
                className={`flex items-center gap-2 text-sm ${
                  message.type === "success" ? "text-green-600" : "text-red-600"
                }`}
              >
                {message.type === "success" ? (
                  <CheckCircle className="w-4 h-4" />
                ) : (
                  <AlertCircle className="w-4 h-4" />
                )}
                {message.text}
              </div>
            )}
          </div>
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-2 px-5 py-2.5 bg-blue-600 text-white rounded-lg font-medium
              hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors cursor-pointer"
          >
            {saving ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            Enregistrer
          </button>
        </div>
      </div>
    </div>
  );
}
