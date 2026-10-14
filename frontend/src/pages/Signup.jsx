import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Rocket, User, Shield, Crown } from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { signup } from "../services/api";

export default function Signup() {
  const { firebaseUser, isAuthenticated, isRegistered, isApproved, loading, refetchProfile } = useAuth();
  const navigate = useNavigate();
  const [selectedRole, setSelectedRole] = useState("simple_user");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (loading) return;
    if (!isAuthenticated) {
      navigate("/login", { replace: true });
      return;
    }
    if (isRegistered && isApproved) {
      navigate("/", { replace: true });
    } else if (isRegistered) {
      navigate("/pending", { replace: true });
    }
  }, [loading, isAuthenticated, isRegistered, isApproved, navigate]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);

    try {
      await signup(selectedRole);
      await refetchProfile();
      navigate("/pending", { replace: true });
    } catch (err) {
      setError(err.response?.data?.detail || "Inscription échouée. Réessayez.");
    } finally {
      setSubmitting(false);
    }
  };

  if (loading || !firebaseUser) return null;

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="max-w-lg w-full mx-4">
        <div className="bg-white rounded-xl shadow-lg p-8">
          <div className="flex items-center justify-center gap-2 mb-6">
            <Rocket className="w-7 h-7 text-blue-600" />
            <h1 className="text-2xl font-bold text-gray-900">Inscription</h1>
          </div>

          {/* User info from Google */}
          <div className="flex items-center gap-4 p-4 bg-gray-50 rounded-lg mb-6">
            {firebaseUser.photoURL ? (
              <img
                src={firebaseUser.photoURL}
                alt="Avatar"
                className="w-12 h-12 rounded-full"
                referrerPolicy="no-referrer"
              />
            ) : (
              <div className="w-12 h-12 rounded-full bg-blue-100 flex items-center justify-center">
                <User className="w-6 h-6 text-blue-600" />
              </div>
            )}
            <div>
              <p className="font-semibold text-gray-900">{firebaseUser.displayName}</p>
              <p className="text-sm text-gray-500">{firebaseUser.email}</p>
            </div>
          </div>

          <form onSubmit={handleSubmit} className="space-y-6">
            {/* Role selection */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-3">
                Choisissez votre r&ocirc;le
              </label>
              <div className="space-y-3">
                <label
                  className={`flex items-start gap-4 p-4 rounded-lg border-2 cursor-pointer transition-all ${
                    selectedRole === "simple_user"
                      ? "border-blue-500 bg-blue-50"
                      : "border-gray-200 hover:border-gray-300"
                  }`}
                >
                  <input
                    type="radio"
                    name="role"
                    value="simple_user"
                    checked={selectedRole === "simple_user"}
                    onChange={() => setSelectedRole("simple_user")}
                    className="mt-1"
                  />
                  <div>
                    <div className="flex items-center gap-2">
                      <User className="w-4 h-4 text-blue-600" />
                      <span className="font-semibold text-gray-900">Utilisateur Simple</span>
                    </div>
                    <p className="text-sm text-gray-500 mt-1">
                      D&eacute;ploiement en mode Demo uniquement. Id&eacute;al pour tester vos sites.
                    </p>
                  </div>
                </label>

                <label
                  className={`flex items-start gap-4 p-4 rounded-lg border-2 cursor-pointer transition-all ${
                    selectedRole === "super_user"
                      ? "border-emerald-500 bg-emerald-50"
                      : "border-gray-200 hover:border-gray-300"
                  }`}
                >
                  <input
                    type="radio"
                    name="role"
                    value="super_user"
                    checked={selectedRole === "super_user"}
                    onChange={() => setSelectedRole("super_user")}
                    className="mt-1"
                  />
                  <div>
                    <div className="flex items-center gap-2">
                      <Shield className="w-4 h-4 text-emerald-600" />
                      <span className="font-semibold text-gray-900">Super Utilisateur</span>
                    </div>
                    <p className="text-sm text-gray-500 mt-1">
                      Tous les modes de d&eacute;ploiement : Demo, Production, Cloud Run.
                    </p>
                  </div>
                </label>

                <label
                  className={`flex items-start gap-4 p-4 rounded-lg border-2 cursor-pointer transition-all ${
                    selectedRole === "admin"
                      ? "border-purple-500 bg-purple-50"
                      : "border-gray-200 hover:border-gray-300"
                  }`}
                >
                  <input
                    type="radio"
                    name="role"
                    value="admin"
                    checked={selectedRole === "admin"}
                    onChange={() => setSelectedRole("admin")}
                    className="mt-1"
                  />
                  <div>
                    <div className="flex items-center gap-2">
                      <Crown className="w-4 h-4 text-purple-600" />
                      <span className="font-semibold text-gray-900">Administrateur</span>
                    </div>
                    <p className="text-sm text-gray-500 mt-1">
                      Tous les modes de d&eacute;ploiement + gestion des utilisateurs et statistiques.
                    </p>
                  </div>
                </label>
              </div>
            </div>

            {error && (
              <div className="p-3 bg-red-50 border border-red-200 rounded-lg">
                <p className="text-sm text-red-700">{error}</p>
              </div>
            )}

            <button
              type="submit"
              disabled={submitting}
              className={`w-full py-3 px-6 rounded-lg text-base font-semibold text-white transition-all ${
                submitting
                  ? "bg-gray-300 cursor-not-allowed"
                  : "bg-blue-600 hover:bg-blue-700 shadow-md hover:shadow-lg cursor-pointer"
              }`}
            >
              {submitting ? "Inscription en cours..." : "S'inscrire"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
