import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Clock, RefreshCw, LogOut } from "lucide-react";
import { useAuth } from "../contexts/AuthContext";

export default function Pending() {
  const { isAuthenticated, isApproved, userProfile, loading, refetchProfile, signOut } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (loading) return;
    if (!isAuthenticated) {
      navigate("/login", { replace: true });
      return;
    }
    if (isApproved) {
      navigate("/", { replace: true });
    }
  }, [loading, isAuthenticated, isApproved, navigate]);

  const handleRefresh = async () => {
    await refetchProfile();
  };

  const handleSignOut = async () => {
    await signOut();
    navigate("/login", { replace: true });
  };

  if (loading) return null;

  const roleLabels = {
    simple_user: "Utilisateur Simple",
    super_user: "Super Utilisateur",
  };

  const isRejected = userProfile?.status === "rejected";

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="max-w-md w-full mx-4">
        <div className="bg-white rounded-xl shadow-lg p-8 text-center">
          {isRejected ? (
            <>
              <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-red-100 flex items-center justify-center">
                <span className="text-2xl">&#10060;</span>
              </div>
              <h1 className="text-2xl font-bold text-gray-900 mb-2">
                Demande refus&eacute;e
              </h1>
              <p className="text-gray-600 mb-6">
                Votre demande d'acc&egrave;s a &eacute;t&eacute; refus&eacute;e.
                Contactez l'administrateur si vous pensez qu'il s'agit d'une erreur.
              </p>
            </>
          ) : (
            <>
              <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-amber-100 flex items-center justify-center">
                <Clock className="w-8 h-8 text-amber-600" />
              </div>
              <h1 className="text-2xl font-bold text-gray-900 mb-2">
                En attente d'approbation
              </h1>
              <p className="text-gray-600 mb-2">
                Votre compte est en attente d'approbation par l'administrateur.
              </p>
              {userProfile?.requested_role && (
                <p className="text-sm text-gray-500 mb-6">
                  R&ocirc;le demand&eacute; : <strong>{roleLabels[userProfile.requested_role] || userProfile.requested_role}</strong>
                </p>
              )}
            </>
          )}

          <div className="flex gap-3 justify-center">
            <button
              onClick={handleRefresh}
              className="flex items-center gap-2 px-4 py-2 rounded-lg border border-gray-300
                text-gray-700 hover:bg-gray-50 transition-colors cursor-pointer"
            >
              <RefreshCw className="w-4 h-4" />
              V&eacute;rifier le statut
            </button>
            <button
              onClick={handleSignOut}
              className="flex items-center gap-2 px-4 py-2 rounded-lg border border-gray-300
                text-gray-700 hover:bg-gray-50 transition-colors cursor-pointer"
            >
              <LogOut className="w-4 h-4" />
              Se d&eacute;connecter
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
