import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import {
  Loader2, GitBranch, Plus, Trash2, Rocket, ExternalLink,
  Copy, CheckCircle, AlertCircle, X, GitCommit
} from "lucide-react";
import {
  getGitConnections, createGitConnection, deleteGitConnection,
  getGitPushEvents, updateGitBranch
} from "../services/api";

export default function GitIntegration() {
  const navigate = useNavigate();
  const [connections, setConnections] = useState([]);
  const [pushEvents, setPushEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    provider: "github",
    repo_url: "",
    repo_name: "",
    branch: "main",
    access_token: "",
  });
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const [conns, events] = await Promise.all([
        getGitConnections(),
        getGitPushEvents(),
      ]);
      setConnections(conns);
      setPushEvents(events);
    } catch {
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const handleCreate = async (e) => {
    e.preventDefault();
    setCreating(true);
    setError(null);
    try {
      await createGitConnection(form);
      setShowForm(false);
      setForm({ provider: "github", repo_url: "", repo_name: "", branch: "main", access_token: "" });
      fetchData();
    } catch (err) {
      setError(err.response?.data?.detail || "Erreur lors de la connexion.");
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id) => {
    if (!confirm("Supprimer cette connexion Git ?")) return;
    try {
      await deleteGitConnection(id);
      fetchData();
    } catch {}
  };

  const handleCopy = (text, id) => {
    navigator.clipboard.writeText(text);
    setCopied(id);
    setTimeout(() => setCopied(null), 2000);
  };

  const handleDeploy = (event) => {
    // Navigate to deploy page with pre-filled info
    navigate(`/?from_git=${event.id}&commit=${event.commit_sha}`);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-8 h-8 animate-spin text-blue-600" />
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Integration Git</h1>
          <p className="text-sm text-gray-500 mt-1">
            Connectez vos repos GitHub/GitLab et deployez sur chaque push
          </p>
        </div>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 cursor-pointer"
        >
          {showForm ? <X className="w-4 h-4" /> : <Plus className="w-4 h-4" />}
          {showForm ? "Annuler" : "Connecter un repo"}
        </button>
      </div>

      {/* New connection form */}
      {showForm && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="font-semibold text-gray-900 mb-4">Nouveau repo</h2>
          <form onSubmit={handleCreate} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Provider</label>
                <div className="flex gap-2">
                  {["github", "gitlab"].map((p) => (
                    <button
                      key={p}
                      type="button"
                      onClick={() => setForm({ ...form, provider: p })}
                      className={`flex-1 py-2 px-3 rounded-lg text-sm font-medium border transition-colors cursor-pointer ${
                        form.provider === p
                          ? "bg-gray-900 text-white border-gray-900"
                          : "bg-white text-gray-700 border-gray-300 hover:bg-gray-50"
                      }`}
                    >
                      {p === "github" ? "GitHub" : "GitLab"}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Branche</label>
                <input
                  type="text"
                  value={form.branch}
                  onChange={(e) => setForm({ ...form, branch: e.target.value })}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Nom du repo</label>
              <input
                type="text"
                value={form.repo_name}
                onChange={(e) => setForm({ ...form, repo_name: e.target.value })}
                placeholder="mon-projet"
                required
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">URL du repo</label>
              <input
                type="url"
                value={form.repo_url}
                onChange={(e) => setForm({ ...form, repo_url: e.target.value })}
                placeholder="https://github.com/user/repo"
                required
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Access Token (Personal Access Token)
              </label>
              <input
                type="password"
                value={form.access_token}
                onChange={(e) => setForm({ ...form, access_token: e.target.value })}
                placeholder="ghp_xxxxxxxxxxxx"
                required
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <p className="text-xs text-gray-400 mt-1">
                Necessite les permissions: repo (read). Le token est stocke de maniere securisee.
              </p>
            </div>
            {error && (
              <div className="flex items-center gap-2 text-sm text-red-600">
                <AlertCircle className="w-4 h-4" /> {error}
              </div>
            )}
            <button
              type="submit"
              disabled={creating}
              className="flex items-center gap-2 px-5 py-2.5 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 cursor-pointer"
            >
              {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitBranch className="w-4 h-4" />}
              Connecter
            </button>
          </form>
        </div>
      )}

      {/* Connected repos */}
      {connections.length === 0 && !showForm ? (
        <div className="text-center py-12 bg-white rounded-lg border border-gray-200">
          <GitBranch className="w-12 h-12 text-gray-300 mx-auto mb-4" />
          <p className="text-gray-500">Aucun repo connecte</p>
          <p className="text-sm text-gray-400 mt-1">
            Connectez un repo pour recevoir des notifications a chaque push
          </p>
        </div>
      ) : (
        connections.map((conn) => (
          <div key={conn.id} className="bg-white rounded-lg border border-gray-200 p-6">
            <div className="flex items-start justify-between mb-4">
              <div>
                <div className="flex items-center gap-2">
                  <GitBranch className="w-5 h-5 text-gray-600" />
                  <h3 className="font-semibold text-gray-900">{conn.repo_name}</h3>
                  <span className="text-xs px-2 py-0.5 bg-gray-100 rounded text-gray-600">
                    {conn.provider}
                  </span>
                  <span className="text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded font-medium">
                    {conn.branch}
                  </span>
                </div>
                <a
                  href={conn.repo_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-500 hover:underline flex items-center gap-1 mt-1"
                >
                  {conn.repo_url} <ExternalLink className="w-3 h-3" />
                </a>
              </div>
              <button
                onClick={() => handleDelete(conn.id)}
                className="p-2 text-gray-400 hover:text-red-500 cursor-pointer"
                title="Supprimer"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>

            {/* Webhook URL */}
            <div className="bg-gray-50 rounded-lg p-3 mb-4">
              <p className="text-xs font-medium text-gray-500 mb-1">Webhook URL (a configurer dans {conn.provider})</p>
              <div className="flex items-center gap-2">
                <code className="text-xs text-gray-700 bg-white px-2 py-1 rounded border flex-1 overflow-x-auto">
                  {conn.webhook_url}
                </code>
                <button
                  onClick={() => handleCopy(conn.webhook_url, `url-${conn.id}`)}
                  className="p-1.5 text-gray-400 hover:text-gray-600 cursor-pointer"
                >
                  {copied === `url-${conn.id}` ? (
                    <CheckCircle className="w-4 h-4 text-green-500" />
                  ) : (
                    <Copy className="w-4 h-4" />
                  )}
                </button>
              </div>
              <div className="flex items-center gap-2 mt-2">
                <span className="text-xs text-gray-500">Secret:</span>
                <code className="text-xs text-gray-700 bg-white px-2 py-1 rounded border">
                  {conn.webhook_secret}
                </code>
                <button
                  onClick={() => handleCopy(conn.webhook_secret, `secret-${conn.id}`)}
                  className="p-1.5 text-gray-400 hover:text-gray-600 cursor-pointer"
                >
                  {copied === `secret-${conn.id}` ? (
                    <CheckCircle className="w-4 h-4 text-green-500" />
                  ) : (
                    <Copy className="w-4 h-4" />
                  )}
                </button>
              </div>
            </div>

            {/* Push events for this connection */}
            {(() => {
              const connEvents = pushEvents.filter((e) => e.connection_id === conn.id);
              if (connEvents.length === 0) {
                return (
                  <p className="text-sm text-gray-400 text-center py-3">
                    Aucun push detecte pour le moment
                  </p>
                );
              }
              return (
                <div className="space-y-2">
                  <p className="text-xs font-medium text-gray-500">Derniers pushes</p>
                  {connEvents.slice(0, 5).map((event) => (
                    <div
                      key={event.id}
                      className={`flex items-center justify-between p-3 rounded-lg border ${
                        event.deployed ? "bg-green-50 border-green-200" : "bg-amber-50 border-amber-200"
                      }`}
                    >
                      <div className="flex items-center gap-3">
                        <GitCommit className="w-4 h-4 text-gray-500" />
                        <div>
                          <p className="text-sm font-medium text-gray-900">
                            {event.commit_message.split("\n")[0]}
                          </p>
                          <p className="text-xs text-gray-500">
                            {event.commit_sha} par {event.author}
                            {event.timestamp && ` - ${new Date(event.timestamp).toLocaleString("fr")}`}
                          </p>
                        </div>
                      </div>
                      {event.deployed ? (
                        <span className="text-xs px-2 py-1 bg-green-100 text-green-700 rounded font-medium">
                          Deploye
                        </span>
                      ) : (
                        <button
                          onClick={() => handleDeploy(event)}
                          className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 text-white text-xs rounded-lg font-medium hover:bg-blue-700 cursor-pointer"
                        >
                          <Rocket className="w-3.5 h-3.5" />
                          Deployer
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              );
            })()}
          </div>
        ))
      )}
    </div>
  );
}
