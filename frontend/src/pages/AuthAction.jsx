import { useEffect, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { CheckCircle, XCircle, Loader } from "lucide-react";
import { approveUser, rejectUser } from "../services/api";

export default function AuthAction() {
  const [searchParams] = useSearchParams();
  const [status, setStatus] = useState("loading"); // loading | success | error
  const [message, setMessage] = useState("");

  const uid = searchParams.get("uid");
  const action = searchParams.get("action");
  const token = searchParams.get("token");

  useEffect(() => {
    if (!uid || !action || !token) {
      setStatus("error");
      setMessage("Lien invalide — paramètres manquants.");
      return;
    }

    const performAction = async () => {
      try {
        if (action === "approve") {
          await approveUser(uid, { token });
          setMessage("L'utilisateur a été approuvé avec succès.");
        } else if (action === "reject") {
          await rejectUser(uid, token);
          setMessage("L'utilisateur a été rejeté.");
        } else {
          setMessage(`Action inconnue : ${action}`);
          setStatus("error");
          return;
        }
        setStatus("success");
      } catch (err) {
        const detail = err.response?.data?.detail || "Une erreur est survenue.";
        setMessage(detail);
        setStatus("error");
      }
    };

    performAction();
  }, [uid, action, token]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="max-w-md w-full mx-4">
        <div className="bg-white rounded-xl shadow-lg p-8 text-center">
          {status === "loading" && (
            <>
              <Loader className="w-12 h-12 mx-auto mb-4 text-blue-600 animate-spin" />
              <p className="text-gray-600">Traitement en cours...</p>
            </>
          )}

          {status === "success" && (
            <>
              <CheckCircle className="w-12 h-12 mx-auto mb-4 text-green-600" />
              <h1 className="text-xl font-bold text-gray-900 mb-2">
                {action === "approve" ? "Approuvé" : "Rejeté"}
              </h1>
              <p className="text-gray-600 mb-6">{message}</p>
            </>
          )}

          {status === "error" && (
            <>
              <XCircle className="w-12 h-12 mx-auto mb-4 text-red-600" />
              <h1 className="text-xl font-bold text-gray-900 mb-2">Erreur</h1>
              <p className="text-gray-600 mb-6">{message}</p>
            </>
          )}

          <Link
            to="/login"
            className="inline-block px-6 py-2 rounded-lg bg-blue-600 text-white font-semibold
              hover:bg-blue-700 transition-colors"
          >
            Aller &agrave; WebDeploy
          </Link>
        </div>
      </div>
    </div>
  );
}
