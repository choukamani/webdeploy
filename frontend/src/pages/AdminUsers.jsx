import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import {
  Users, CheckCircle, XCircle, Clock, Shield, Loader,
  UserPlus, Trash2, ChevronDown, X,
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import {
  listUsers, approveUser, rejectUser,
  updateUserRole, adminCreateUser, adminDeleteUser,
} from "../services/api";

const ROLE_OPTIONS = [
  { value: "admin", label: "Admin" },
  { value: "super_user", label: "Super User" },
  { value: "simple_user", label: "Simple User" },
];

const roleLabels = {
  admin: "Admin",
  super_user: "Super User",
  simple_user: "Simple User",
};

const roleBadgeColors = {
  admin: "bg-purple-100 text-purple-800",
  super_user: "bg-emerald-100 text-emerald-800",
  simple_user: "bg-blue-100 text-blue-800",
};

const statusBadgeColors = {
  approved: "bg-green-100 text-green-800",
  pending: "bg-amber-100 text-amber-800",
  rejected: "bg-red-100 text-red-800",
};

export default function AdminUsers() {
  const { isAdmin, loading: authLoading, userProfile } = useAuth();
  const navigate = useNavigate();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(null);
  const [approveMenuOpen, setApproveMenuOpen] = useState(null); // uid or null
  const [showCreate, setShowCreate] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(null); // user object or null
  const [error, setError] = useState(null);

  const fetchUsers = useCallback(async () => {
    try {
      const data = await listUsers();
      setUsers(data);
    } catch (err) {
      console.error("Failed to fetch users:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (authLoading) return;
    if (!isAdmin) {
      navigate("/", { replace: true });
      return;
    }
    fetchUsers();
  }, [authLoading, isAdmin, navigate, fetchUsers]);

  // Close approve menu when clicking outside
  useEffect(() => {
    if (!approveMenuOpen) return;
    const handler = () => setApproveMenuOpen(null);
    window.addEventListener("click", handler);
    return () => window.removeEventListener("click", handler);
  }, [approveMenuOpen]);

  const handleApprove = async (uid, role) => {
    setError(null);
    setActionLoading(uid);
    setApproveMenuOpen(null);
    try {
      await approveUser(uid, role ? { role } : {});
      await fetchUsers();
    } catch (err) {
      setError(err.response?.data?.detail || "Echec de l'approbation.");
    } finally {
      setActionLoading(null);
    }
  };

  const handleReject = async (uid) => {
    setError(null);
    setActionLoading(uid);
    try {
      await rejectUser(uid);
      await fetchUsers();
    } catch (err) {
      setError(err.response?.data?.detail || "Echec du rejet.");
    } finally {
      setActionLoading(null);
    }
  };

  const handleRoleChange = async (uid, newRole) => {
    setError(null);
    setActionLoading(uid);
    try {
      await updateUserRole(uid, newRole);
      await fetchUsers();
    } catch (err) {
      setError(err.response?.data?.detail || "Echec du changement de role.");
    } finally {
      setActionLoading(null);
    }
  };

  const handleDelete = async (uid) => {
    setError(null);
    setActionLoading(uid);
    try {
      await adminDeleteUser(uid);
      setConfirmDelete(null);
      await fetchUsers();
    } catch (err) {
      setError(err.response?.data?.detail || "Echec de la suppression.");
    } finally {
      setActionLoading(null);
    }
  };

  if (authLoading || loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader className="w-6 h-6 animate-spin text-blue-600" />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Users className="w-7 h-7 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900">Gestion des Utilisateurs</h1>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg
            text-sm font-semibold hover:bg-blue-700 transition-colors cursor-pointer shadow-sm"
        >
          <UserPlus className="w-4 h-4" />
          Ajouter un utilisateur
        </button>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-visible">
        <table className="w-full">
          <thead>
            <tr className="bg-gray-50 border-b border-gray-200">
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Utilisateur</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">R&ocirc;le demand&eacute;</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">R&ocirc;le</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Statut</th>
              <th className="text-left py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Date</th>
              <th className="text-right py-3 px-4 text-xs font-semibold text-gray-500 uppercase">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const isSelf = userProfile && u.uid === userProfile.uid;
              return (
                <tr key={u.uid} className="border-b border-gray-100 hover:bg-gray-50">
                  <td className="py-3 px-4">
                    <div className="font-medium text-gray-900">{u.display_name || "—"}</div>
                    <div className="text-sm text-gray-500">{u.email}</div>
                  </td>
                  <td className="py-3 px-4">
                    <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-semibold ${roleBadgeColors[u.requested_role] || "bg-gray-100 text-gray-800"}`}>
                      {roleLabels[u.requested_role] || u.requested_role}
                    </span>
                  </td>
                  <td className="py-3 px-4">
                    {u.status === "approved" && u.role ? (
                      <select
                        value={u.role}
                        onChange={(e) => handleRoleChange(u.uid, e.target.value)}
                        disabled={actionLoading === u.uid || isSelf}
                        className={`px-2 py-1 rounded-md text-xs font-semibold border border-gray-200 bg-white
                          focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50
                          ${isSelf ? "cursor-not-allowed" : "cursor-pointer"}`}
                        title={isSelf ? "Vous ne pouvez pas changer votre propre role." : ""}
                      >
                        {ROLE_OPTIONS.map((opt) => (
                          <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                      </select>
                    ) : u.role ? (
                      <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-semibold ${roleBadgeColors[u.role] || "bg-gray-100 text-gray-800"}`}>
                        {u.role === "admin" && <Shield className="w-3 h-3" />}
                        {roleLabels[u.role] || u.role}
                      </span>
                    ) : (
                      <span className="text-gray-400 text-sm">—</span>
                    )}
                  </td>
                  <td className="py-3 px-4">
                    <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-semibold ${statusBadgeColors[u.status] || "bg-gray-100 text-gray-800"}`}>
                      {u.status === "approved" && <CheckCircle className="w-3 h-3" />}
                      {u.status === "pending" && <Clock className="w-3 h-3" />}
                      {u.status === "rejected" && <XCircle className="w-3 h-3" />}
                      {u.status}
                    </span>
                  </td>
                  <td className="py-3 px-4 text-sm text-gray-500">
                    {u.created_at ? new Date(u.created_at).toLocaleDateString("fr-FR") : "—"}
                  </td>
                  <td className="py-3 px-4 text-right">
                    <div className="flex gap-2 justify-end items-center">
                      {u.status === "pending" && (
                        <>
                          <div className="relative inline-block">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setApproveMenuOpen(approveMenuOpen === u.uid ? null : u.uid);
                              }}
                              disabled={actionLoading === u.uid}
                              className="flex items-center gap-1 px-3 py-1.5 rounded-md text-xs font-semibold bg-green-50 text-green-700
                                hover:bg-green-100 transition-colors cursor-pointer disabled:opacity-50"
                            >
                              Approuver <ChevronDown className="w-3 h-3" />
                            </button>
                            {approveMenuOpen === u.uid && (
                              <div className="absolute right-0 mt-1 w-48 bg-white border border-gray-200 rounded-md shadow-lg z-20"
                                   onClick={(e) => e.stopPropagation()}>
                                <button
                                  onClick={() => handleApprove(u.uid, null)}
                                  className="w-full text-left px-3 py-2 text-xs hover:bg-gray-50 cursor-pointer
                                    border-b border-gray-100"
                                >
                                  Approuver tel quel
                                  <span className="block text-[10px] text-gray-400">
                                    R&ocirc;le demand&eacute;: {roleLabels[u.requested_role] || u.requested_role}
                                  </span>
                                </button>
                                {ROLE_OPTIONS.map((opt) => (
                                  <button
                                    key={opt.value}
                                    onClick={() => handleApprove(u.uid, opt.value)}
                                    className="w-full text-left px-3 py-2 text-xs hover:bg-gray-50 cursor-pointer"
                                  >
                                    Approuver comme <span className="font-semibold">{opt.label}</span>
                                  </button>
                                ))}
                              </div>
                            )}
                          </div>
                          <button
                            onClick={() => handleReject(u.uid)}
                            disabled={actionLoading === u.uid}
                            className="px-3 py-1.5 rounded-md text-xs font-semibold bg-red-50 text-red-700
                              hover:bg-red-100 transition-colors cursor-pointer disabled:opacity-50"
                          >
                            Rejeter
                          </button>
                        </>
                      )}
                      {!isSelf && (
                        <button
                          onClick={() => setConfirmDelete(u)}
                          disabled={actionLoading === u.uid}
                          className="p-1.5 rounded-md text-gray-400 hover:text-red-600 hover:bg-red-50
                            transition-colors cursor-pointer disabled:opacity-50"
                          title="Supprimer"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
            {users.length === 0 && (
              <tr>
                <td colSpan={6} className="py-8 text-center text-gray-500">
                  Aucun utilisateur enregistr&eacute;.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {showCreate && (
        <CreateUserModal
          onClose={() => setShowCreate(false)}
          onCreated={async () => {
            setShowCreate(false);
            await fetchUsers();
          }}
          onError={setError}
        />
      )}

      {confirmDelete && (
        <ConfirmDeleteModal
          user={confirmDelete}
          onCancel={() => setConfirmDelete(null)}
          onConfirm={() => handleDelete(confirmDelete.uid)}
          loading={actionLoading === confirmDelete.uid}
        />
      )}
    </div>
  );
}


// ── Create user modal ────────────────────────────────────────────────

function CreateUserModal({ onClose, onCreated, onError }) {
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("simple_user");
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLocalError(null);
    const trimmed = email.trim().toLowerCase();
    if (!trimmed || !trimmed.includes("@")) {
      setLocalError("Email invalide.");
      return;
    }
    setSubmitting(true);
    try {
      await adminCreateUser({
        email: trimmed,
        display_name: displayName.trim(),
        role,
      });
      onCreated();
    } catch (err) {
      const detail = err.response?.data?.detail || "Echec de la creation.";
      setLocalError(detail);
      onError?.(null);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
          <h2 className="font-semibold text-gray-900 flex items-center gap-2">
            <UserPlus className="w-5 h-5 text-blue-600" />
            Ajouter un utilisateur
          </h2>
          <button
            onClick={onClose}
            className="p-1 rounded-md hover:bg-gray-100 cursor-pointer"
          >
            <X className="w-5 h-5 text-gray-500" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Email <span className="text-red-500">*</span>
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="jean.dupont@example.com"
              className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm
                focus:outline-none focus:ring-2 focus:ring-blue-500"
              required
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Nom complet
            </label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Jean Dupont"
              className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm
                focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              R&ocirc;le <span className="text-red-500">*</span>
            </label>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm bg-white
                focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {ROLE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
          <p className="text-xs text-gray-500">
            L&apos;utilisateur est cr&eacute;&eacute; approuv&eacute; avec le r&ocirc;le choisi.
            Il pourra se connecter via Google Sign-In avec cet email.
          </p>
          {localError && (
            <div className="p-2 bg-red-50 border border-red-200 rounded text-xs text-red-700">
              {localError}
            </div>
          )}
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded-md text-sm font-medium text-gray-700
                border border-gray-300 hover:bg-gray-50 cursor-pointer"
            >
              Annuler
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 rounded-md text-sm font-semibold text-white
                bg-blue-600 hover:bg-blue-700 disabled:opacity-50 cursor-pointer
                flex items-center gap-2"
            >
              {submitting && <Loader className="w-4 h-4 animate-spin" />}
              Cr&eacute;er
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}


// ── Confirm delete modal ─────────────────────────────────────────────

function ConfirmDeleteModal({ user, onCancel, onConfirm, loading }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4">
        <div className="px-6 py-4 border-b border-gray-200">
          <h2 className="font-semibold text-gray-900 flex items-center gap-2">
            <Trash2 className="w-5 h-5 text-red-600" />
            Supprimer l&apos;utilisateur
          </h2>
        </div>
        <div className="p-6 space-y-3">
          <p className="text-sm text-gray-700">
            Voulez-vous vraiment supprimer <span className="font-semibold">{user.display_name || user.email}</span> ?
          </p>
          <p className="text-xs text-gray-500">
            Cette action supprime le compte de Firebase Auth et de Firestore. Elle est irr&eacute;versible.
          </p>
        </div>
        <div className="px-6 py-3 border-t border-gray-200 flex justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={loading}
            className="px-4 py-2 rounded-md text-sm font-medium text-gray-700
              border border-gray-300 hover:bg-gray-50 cursor-pointer disabled:opacity-50"
          >
            Annuler
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className="px-4 py-2 rounded-md text-sm font-semibold text-white
              bg-red-600 hover:bg-red-700 disabled:opacity-50 cursor-pointer
              flex items-center gap-2"
          >
            {loading && <Loader className="w-4 h-4 animate-spin" />}
            Supprimer
          </button>
        </div>
      </div>
    </div>
  );
}
