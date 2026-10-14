import { Navigate } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";

/**
 * Route guard that checks authentication state and redirects accordingly.
 *
 * @param {object} props
 * @param {React.ReactNode} props.children - The protected content.
 * @param {string} [props.requiredRole] - Optional role requirement ("admin").
 */
export default function ProtectedRoute({ children, requiredRole }) {
  const { isAuthenticated, isRegistered, isApproved, isAdmin, isSuperUser, loading, profileError } = useAuth();

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
      </div>
    );
  }

  // Not authenticated → login
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  // Authenticated but not registered → signup
  if (profileError === "not_registered" || !isRegistered) {
    return <Navigate to="/signup" replace />;
  }

  // Registered but not approved → pending
  if (!isApproved) {
    return <Navigate to="/pending" replace />;
  }

  // Role check
  if (requiredRole === "admin" && !isAdmin) {
    return <Navigate to="/" replace />;
  }
  if (requiredRole === "super_user" && !isSuperUser && !isAdmin) {
    return <Navigate to="/" replace />;
  }

  return children;
}
