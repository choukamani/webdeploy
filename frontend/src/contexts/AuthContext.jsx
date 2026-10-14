import { createContext, useContext, useEffect, useState, useCallback } from "react";
import { onAuthStateChanged, signInWithCustomToken, signOut as firebaseSignOut } from "firebase/auth";
import { auth } from "../config/firebase";
import api, { getMe } from "../services/api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [firebaseUser, setFirebaseUser] = useState(null);
  const [userProfile, setUserProfile] = useState(null);
  const [loading, setLoading] = useState(true);
  const [profileError, setProfileError] = useState(null);

  // Set up axios interceptor: attach Bearer token to every request
  useEffect(() => {
    const interceptor = api.interceptors.request.use(async (config) => {
      const currentUser = auth.currentUser;
      if (currentUser) {
        try {
          const token = await currentUser.getIdToken();
          config.headers.Authorization = `Bearer ${token}`;
        } catch {
          // Token refresh failed — let the request go without auth
        }
      }
      return config;
    });
    return () => api.interceptors.request.eject(interceptor);
  }, []);

  // Listen to Firebase auth state changes
  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, async (user) => {
      setFirebaseUser(user);
      if (user) {
        // Fetch backend profile
        try {
          const profile = await getMe();
          setUserProfile(profile);
          setProfileError(null);
        } catch (err) {
          const detail = err.response?.data?.detail;
          if (detail === "not_registered") {
            // User exists in Firebase but hasn't signed up in our system
            setUserProfile(null);
            setProfileError("not_registered");
          } else {
            setUserProfile(null);
            setProfileError(detail || "unknown");
          }
        }
      } else {
        setUserProfile(null);
        setProfileError(null);
      }
      setLoading(false);
    });
    return unsubscribe;
  }, []);

  // Sign in with a Firebase Custom Token (returned by our backend)
  const signInWithToken = useCallback(async (customToken) => {
    await signInWithCustomToken(auth, customToken);
  }, []);

  const signOutUser = useCallback(async () => {
    await firebaseSignOut(auth);
    setUserProfile(null);
    setProfileError(null);
  }, []);

  const refetchProfile = useCallback(async () => {
    if (!auth.currentUser) return;
    try {
      const profile = await getMe();
      setUserProfile(profile);
      setProfileError(null);
    } catch (err) {
      const detail = err.response?.data?.detail;
      setProfileError(detail || "unknown");
    }
  }, []);

  // Derived flags
  const isAuthenticated = !!firebaseUser;
  const isRegistered = !!userProfile;
  const isApproved = userProfile?.status === "approved";
  const isAdmin = isApproved && userProfile?.role === "admin";
  const isSuperUser = isApproved && userProfile?.role === "super_user";
  const isSimpleUser = isApproved && userProfile?.role === "simple_user";

  const value = {
    firebaseUser,
    userProfile,
    loading,
    profileError,
    signInWithToken,
    signOut: signOutUser,
    refetchProfile,
    isAuthenticated,
    isRegistered,
    isApproved,
    isAdmin,
    isSuperUser,
    isSimpleUser,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
