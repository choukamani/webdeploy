import { useState, useEffect } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  Rocket, Upload, History, BarChart3, Users, LogOut, Settings,
  GitBranch, LayoutDashboard, Gauge, AlertTriangle
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { getUnresolvedAlertCount, getPendingQuotaRequestCount } from "../services/api";

export default function Layout() {
  const { firebaseUser, isAdmin, isSuperUser, signOut } = useAuth();
  const navigate = useNavigate();
  const [alertCount, setAlertCount] = useState(0);
  const [quotaRequestCount, setQuotaRequestCount] = useState(0);

  // Fetch unresolved alert count + pending quota requests for admin badges
  useEffect(() => {
    if (!isAdmin) return;
    const fetch = async () => {
      try {
        const [alerts, quotas] = await Promise.all([
          getUnresolvedAlertCount().catch(() => ({ count: 0 })),
          getPendingQuotaRequestCount().catch(() => ({ count: 0 })),
        ]);
        setAlertCount(alerts.count || 0);
        setQuotaRequestCount(quotas.count || 0);
      } catch {}
    };
    fetch();
    const interval = setInterval(fetch, 30000);
    return () => clearInterval(interval);
  }, [isAdmin]);

  const linkClass = ({ isActive }) =>
    `flex items-center gap-1.5 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
      isActive
        ? "bg-blue-700 text-white"
        : "text-blue-100 hover:bg-blue-600 hover:text-white"
    }`;

  const handleSignOut = async () => {
    await signOut();
    navigate("/login");
  };

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Navigation bar */}
      <nav className="bg-[#2563EB] shadow-lg">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            {/* Logo */}
            <NavLink to="/" className="flex items-center gap-2 text-white">
              <Rocket className="w-6 h-6" />
              <span className="text-xl font-bold tracking-tight">
                WebDeploy
              </span>
            </NavLink>

            {/* Navigation links */}
            <div className="flex items-center gap-1">
              <NavLink to="/" end className={linkClass}>
                <Upload className="w-4 h-4" />
                <span className="hidden md:inline">Deploy</span>
              </NavLink>
              <NavLink to="/history" className={linkClass}>
                <History className="w-4 h-4" />
                <span className="hidden md:inline">History</span>
              </NavLink>
              {(isSuperUser || isAdmin) && (
                <NavLink to="/git" className={linkClass}>
                  <GitBranch className="w-4 h-4" />
                  <span className="hidden md:inline">Git</span>
                </NavLink>
              )}
              <NavLink to="/settings" className={linkClass}>
                <Settings className="w-4 h-4" />
                <span className="hidden lg:inline">Notifications</span>
              </NavLink>
              {isAdmin && (
                <>
                  <NavLink to="/admin/dashboard" className={linkClass}>
                    <LayoutDashboard className="w-4 h-4" />
                    <span className="hidden md:inline">Dashboard</span>
                    {alertCount > 0 && (
                      <span className="ml-1 px-1.5 py-0.5 text-[10px] font-bold bg-red-500 text-white rounded-full leading-none">
                        {alertCount}
                      </span>
                    )}
                  </NavLink>
                  <NavLink to="/statistics" className={linkClass}>
                    <BarChart3 className="w-4 h-4" />
                    <span className="hidden md:inline">Stats</span>
                  </NavLink>
                  <NavLink to="/admin/users" className={linkClass}>
                    <Users className="w-4 h-4" />
                    <span className="hidden md:inline">Users</span>
                  </NavLink>
                  <NavLink to="/admin/quotas" className={linkClass}>
                    <Gauge className="w-4 h-4" />
                    <span className="hidden md:inline">Quotas</span>
                    {quotaRequestCount > 0 && (
                      <span className="ml-1 px-1.5 py-0.5 text-[10px] font-bold bg-amber-400 text-amber-900 rounded-full leading-none">
                        {quotaRequestCount}
                      </span>
                    )}
                  </NavLink>
                </>
              )}
            </div>

            {/* User info + sign out */}
            <div className="flex items-center gap-3">
              {firebaseUser && (
                <div className="flex items-center gap-2">
                  {firebaseUser.photoURL ? (
                    <img
                      src={firebaseUser.photoURL}
                      alt="Avatar"
                      className="w-8 h-8 rounded-full border-2 border-white/30"
                      referrerPolicy="no-referrer"
                    />
                  ) : (
                    <div className="w-8 h-8 rounded-full bg-blue-800 flex items-center justify-center text-white text-sm font-bold">
                      {(firebaseUser.displayName || firebaseUser.email || "?")[0].toUpperCase()}
                    </div>
                  )}
                  <span className="text-sm text-blue-100 hidden sm:inline">
                    {firebaseUser.displayName || firebaseUser.email}
                  </span>
                </div>
              )}
              <button
                onClick={handleSignOut}
                className="flex items-center gap-1.5 px-3 py-2 rounded-md text-sm font-medium
                  text-blue-100 hover:bg-blue-600 hover:text-white transition-colors cursor-pointer"
                title="Se deconnecter"
              >
                <LogOut className="w-4 h-4" />
                <span className="hidden sm:inline">Deconnexion</span>
              </button>
            </div>
          </div>
        </div>
      </nav>

      {/* Main content */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <Outlet />
      </main>
    </div>
  );
}
