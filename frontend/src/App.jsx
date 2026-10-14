import { Routes, Route } from "react-router-dom";
import { AuthProvider } from "./contexts/AuthContext";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import DeploymentForm from "./components/DeploymentForm";
import DeploymentDetail from "./pages/DeploymentDetail";
import DeploymentHistory from "./components/DeploymentHistory";
import Statistics from "./pages/Statistics";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import Pending from "./pages/Pending";
import AdminUsers from "./pages/AdminUsers";
import AuthAction from "./pages/AuthAction";
import Settings from "./pages/Settings";
import GitIntegration from "./pages/GitIntegration";
import AdminDashboard from "./pages/AdminDashboard";
import AdminQuotas from "./pages/AdminQuotas";

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        {/* Public routes (no layout) */}
        <Route path="/login" element={<Login />} />
        <Route path="/signup" element={<Signup />} />
        <Route path="/pending" element={<Pending />} />
        <Route path="/auth/action" element={<AuthAction />} />

        {/* Protected routes (with layout) */}
        <Route element={<Layout />}>
          <Route path="/" element={<ProtectedRoute><DeploymentForm /></ProtectedRoute>} />
          <Route path="/deployments/:id" element={<ProtectedRoute><DeploymentDetail /></ProtectedRoute>} />
          <Route path="/history" element={<ProtectedRoute><DeploymentHistory /></ProtectedRoute>} />
          <Route path="/settings" element={<ProtectedRoute><Settings /></ProtectedRoute>} />
          <Route path="/git" element={<ProtectedRoute requiredRole="super_user"><GitIntegration /></ProtectedRoute>} />
          <Route path="/admin/dashboard" element={<ProtectedRoute requiredRole="admin"><AdminDashboard /></ProtectedRoute>} />
          <Route path="/statistics" element={<ProtectedRoute requiredRole="admin"><Statistics /></ProtectedRoute>} />
          <Route path="/admin/users" element={<ProtectedRoute requiredRole="admin"><AdminUsers /></ProtectedRoute>} />
          <Route path="/admin/quotas" element={<ProtectedRoute requiredRole="admin"><AdminQuotas /></ProtectedRoute>} />
        </Route>
      </Routes>
    </AuthProvider>
  );
}
