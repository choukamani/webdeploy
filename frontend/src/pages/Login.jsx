import { useEffect, useRef, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { Rocket } from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { googleSignIn } from "../services/api";

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID;

/** Dynamically load the Google Identity Services script. */
function loadGisScript() {
  return new Promise((resolve, reject) => {
    if (window.google?.accounts?.id) {
      resolve();
      return;
    }
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.onload = resolve;
    script.onerror = () => reject(new Error("Failed to load Google Sign-In"));
    document.head.appendChild(script);
  });
}

export default function Login() {
  const { signInWithToken, isAuthenticated, isRegistered, isApproved, loading, profileError } = useAuth();
  const navigate = useNavigate();
  const googleBtnRef = useRef(null);
  const callbackRef = useRef(null);
  const [gisReady, setGisReady] = useState(false);
  const [gisError, setGisError] = useState(null);
  const [signingIn, setSigningIn] = useState(false);

  // Redirect when auth state resolves
  useEffect(() => {
    if (loading) return;
    if (!isAuthenticated) return;

    if (profileError === "not_registered" || !isRegistered) {
      navigate("/signup", { replace: true });
    } else if (!isApproved) {
      navigate("/pending", { replace: true });
    } else {
      navigate("/", { replace: true });
    }
  }, [loading, isAuthenticated, isRegistered, isApproved, profileError, navigate]);

  // GIS credential callback (stable ref so GIS always calls the latest version)
  const handleCredentialResponse = useCallback(async (response) => {
    setSigningIn(true);
    setGisError(null);
    try {
      // Send Google credential to our backend → get Firebase Custom Token
      const { custom_token } = await googleSignIn(response.credential);
      // Sign in to Firebase with the custom token
      await signInWithToken(custom_token);
    } catch (err) {
      console.error("Sign-in failed:", err);
      setGisError("Connexion echouee. Veuillez reessayer.");
    } finally {
      setSigningIn(false);
    }
  }, [signInWithToken]);

  // Keep the ref in sync
  callbackRef.current = handleCredentialResponse;

  // Load GIS and render the Google button
  useEffect(() => {
    if (loading || isAuthenticated) return;

    loadGisScript()
      .then(() => {
        window.google.accounts.id.initialize({
          client_id: GOOGLE_CLIENT_ID,
          callback: (resp) => callbackRef.current(resp),
          auto_select: false,
        });
        if (googleBtnRef.current) {
          window.google.accounts.id.renderButton(googleBtnRef.current, {
            theme: "outline",
            size: "large",
            text: "signin_with",
            shape: "rectangular",
            width: 350,
          });
        }
        setGisReady(true);
      })
      .catch((err) => {
        console.error("GIS load error:", err);
        setGisError("Impossible de charger Google Sign-In.");
      });
  }, [loading, isAuthenticated]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="max-w-md w-full mx-4">
        <div className="bg-white rounded-xl shadow-lg p-8 text-center">
          <div className="flex items-center justify-center gap-2 mb-6">
            <Rocket className="w-8 h-8 text-blue-600" />
            <h1 className="text-3xl font-bold text-gray-900">WebDeploy</h1>
          </div>
          <p className="text-gray-600 mb-8">
            Connectez-vous pour acc&eacute;der &agrave; la plateforme de d&eacute;ploiement.
          </p>

          {signingIn ? (
            <div className="flex items-center justify-center gap-2 py-3">
              <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-blue-600" />
              <span className="text-gray-600">Connexion en cours...</span>
            </div>
          ) : (
            <div ref={googleBtnRef} className="flex justify-center" />
          )}

          {!gisReady && !gisError && !signingIn && (
            <div className="flex items-center justify-center gap-2 py-3">
              <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-gray-400" />
              <span className="text-sm text-gray-400">Chargement...</span>
            </div>
          )}

          {gisError && (
            <p className="mt-4 text-sm text-red-600">{gisError}</p>
          )}
        </div>
      </div>
    </div>
  );
}
