import { useCallback, useEffect, useRef, useState, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import {
  ExternalLink,
  ArrowUpDown,
  Inbox,
  RefreshCw,
  Loader,
  Trash2,
  CalendarRange,
  X,
  AlertTriangle,
  CheckCircle2,
} from "lucide-react";
import {
  getDeployments,
  deleteHistoryEntry,
  deleteWebsites,
  bulkDeleteDeployments,
  WEBSITE_DELETE_CHUNK,
} from "../services/api";

const STATUS_BADGE = {
  queued: "bg-gray-100 text-gray-700 ring-gray-300",
  running: "bg-blue-100 text-blue-700 ring-blue-300",
  success: "bg-green-100 text-green-700 ring-green-300",
  failed: "bg-red-100 text-red-700 ring-red-300",
};

const MODE_BADGE = {
  demo: "bg-purple-100 text-purple-700 ring-purple-300",
  prod: "bg-orange-100 text-orange-700 ring-orange-300",
  cloudrun: "bg-teal-100 text-teal-700 ring-teal-300",
  subdomain: "bg-sky-100 text-sky-700 ring-sky-300",
};

const MODE_LABELS = {
  demo: "Demo",
  prod: "Prod",
  cloudrun: "Cloud Run",
  subdomain: "Subdomain",
};

const MODE_OPTIONS = ["demo", "cloudrun", "subdomain", "prod"];

/** Mirrors the server's _DELETABLE_STATUSES — in-flight runs are never swept. */
const DELETABLE = new Set(["success", "failed"]);

/** Above this many, deleting requires typing the word out. */
const TYPE_TO_CONFIRM_ABOVE = 10;

/** Local calendar day as YYYY-MM-DD (not UTC — avoids an off-by-one evening shift). */
function isoDay(date) {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 10);
}

/** Range covering the last `days` days, today included. */
function lastDays(days) {
  const today = new Date();
  const start = new Date();
  start.setDate(start.getDate() - (days - 1));
  return { from: isoDay(start), to: isoDay(today) };
}

const PRESETS = [
  { label: "All time", range: () => ({ from: "", to: "" }) },
  { label: "Last 7 days", range: () => lastDays(7) },
  { label: "Last 30 days", range: () => lastDays(30) },
  { label: "Last 90 days", range: () => lastDays(90) },
];

function errorText(err, fallback) {
  return err.response?.data?.detail || err.message || fallback;
}

/* ═══════════════════════════════════════════════════════════════════════
   Delete dialog — single or bulk. Previews the *websites* the selection
   resolves to (deleting a deployment deletes the website it built, with all
   of its versions), then deletes them in chunks with visible progress.
   ═══════════════════════════════════════════════════════════════════════ */

const RESULT_STYLE = {
  deleted: "text-green-700",
  delete_failed: "text-red-700",
  blocked: "text-amber-700",
};

function WebsiteRow({ site, result }) {
  return (
    <li className="py-2.5 flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-gray-900">{site.website_name}</span>
        <span
          className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium
            ring-1 ring-inset ${MODE_BADGE[site.mode] || MODE_BADGE.demo}`}
        >
          {MODE_LABELS[site.mode] || site.mode}
        </span>
        {site.live && (
          <span className="text-[11px] font-medium text-green-700 bg-green-50 px-1.5 py-0.5 rounded">
            live
          </span>
        )}
        <span className="text-xs text-gray-500">
          {site.versions} version{site.versions !== 1 ? "s" : ""}
        </span>
        {result && (
          <span className={`ml-auto text-xs font-medium ${RESULT_STYLE[result.status] || ""}`}>
            {result.status === "deleted"
              ? "Deleted"
              : result.status === "delete_failed"
              ? "Failed — retry to resume"
              : "Not deleted"}
          </span>
        )}
      </div>
      {site.url && <span className="text-xs text-gray-500 break-all">{site.url}</span>}
      {!site.deletable && site.blocked_reason && (
        <span className="text-xs text-amber-700">{site.blocked_reason}</span>
      )}
      {site.status === "delete_failed" && site.last_delete_error && !result && (
        <span className="text-xs text-red-600">Previous attempt failed: {site.last_delete_error}</span>
      )}
      {result?.error && <span className="text-xs text-red-600">{result.error}</span>}
      {result?.warnings?.length > 0 && (
        <span className="text-xs text-amber-700">Kept (shared): {result.warnings.join("; ")}</span>
      )}
      {site.resources?.length > 0 && !result && (
        <details className="text-xs text-gray-500">
          <summary className="cursor-pointer select-none hover:text-gray-700">
            {site.resources.length} resource{site.resources.length !== 1 ? "s" : ""} to remove
          </summary>
          <ul className="mt-1 ml-4 list-disc">
            {site.resources.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}

function DeleteDialog({ request, onClose, onCompleted }) {
  const [preview, setPreview] = useState(null);
  const [results, setResults] = useState({}); // key -> server result
  const [recordResult, setRecordResult] = useState(null);
  const [progress, setProgress] = useState(null); // { done, total }
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(true);
  const [confirmText, setConfirmText] = useState("");
  const [scope, setScope] = useState("website"); // "website" | "record"

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    setError(null);
    bulkDeleteDeployments({ ...request.payload, dryRun: true })
      .then((data) => !cancelled && setPreview(data))
      .catch((err) => !cancelled && setError(errorText(err, "Preview failed")))
      .finally(() => !cancelled && setBusy(false));
    return () => {
      cancelled = true;
    };
  }, [request]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const websites = preview?.websites || [];
  const deletable = websites.filter((w) => w.deletable);
  const recordOnlyAllowed =
    Boolean(request.single) && websites.length === 1 && websites[0].record_only_allowed;
  const finished = recordResult || Object.keys(results).length > 0;
  const failedKeys = Object.values(results)
    .filter((r) => r.status === "delete_failed")
    .map((r) => r.key);

  const needsTyping =
    scope === "website" &&
    (deletable.length > TYPE_TO_CONFIRM_ABOVE || deletable.some((w) => w.mode === "prod"));
  const canRun =
    !busy &&
    !finished &&
    (scope === "record" || deletable.length > 0) &&
    (!needsTyping || confirmText === "DELETE");

  const runWebsiteDeletes = async (keys) => {
    setBusy(true);
    setError(null);
    setProgress({ done: 0, total: keys.length });
    try {
      for (let i = 0; i < keys.length; i += WEBSITE_DELETE_CHUNK) {
        const chunk = keys.slice(i, i + WEBSITE_DELETE_CHUNK);
        try {
          const data = await deleteWebsites(chunk);
          setResults((prev) => {
            const next = { ...prev };
            data.results.forEach((r) => (next[r.key] = r));
            return next;
          });
        } catch (err) {
          // The whole chunk failed in transit; mark it so it can be retried.
          const message = errorText(err, "Request failed");
          setResults((prev) => {
            const next = { ...prev };
            chunk.forEach((key) => (next[key] = { key, status: "delete_failed", error: message }));
            return next;
          });
        }
        setProgress({ done: Math.min(i + chunk.length, keys.length), total: keys.length });
      }
      onCompleted();
    } finally {
      setBusy(false);
    }
  };

  const execute = async () => {
    if (scope === "record") {
      setBusy(true);
      setError(null);
      try {
        setRecordResult(await deleteHistoryEntry(request.single.id));
        onCompleted();
      } catch (err) {
        setError(errorText(err, "Delete failed"));
      } finally {
        setBusy(false);
      }
      return;
    }
    await runWebsiteDeletes(deletable.map((w) => w.key));
  };

  const counts = Object.values(results).reduce((acc, r) => {
    acc[r.status] = (acc[r.status] || 0) + 1;
    return acc;
  }, {});

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={() => !busy && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-title"
        onClick={(e) => e.stopPropagation()}
        className="bg-white rounded-xl shadow-xl w-full max-w-2xl max-h-[85vh] flex flex-col"
      >
        <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-gray-200">
          <div className="flex items-start gap-3">
            <div className="mt-0.5 p-2 rounded-lg bg-red-50">
              <Trash2 className="w-5 h-5 text-red-600" />
            </div>
            <div>
              <h2 id="delete-title" className="text-lg font-semibold text-gray-900">
                {finished ? "Deletion finished" : request.single ? "Delete website" : "Delete websites"}
              </h2>
              <p className="text-sm text-gray-500">{request.description}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            className="text-gray-400 hover:text-gray-600 disabled:opacity-40
              disabled:cursor-not-allowed transition-colors"
            aria-label="Close"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-6 py-4 overflow-y-auto grow flex flex-col gap-4">
          {busy && !preview && (
            <div className="flex items-center justify-center py-12">
              <Loader className="w-7 h-7 text-[#2563EB] animate-spin" />
            </div>
          )}

          {error && (
            <div className="px-4 py-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">
              {error}
            </div>
          )}

          {recordResult && (
            <div className="flex items-center gap-2 px-4 py-3 rounded-lg border text-sm bg-green-50 border-green-200 text-green-800">
              <CheckCircle2 className="w-4 h-4 shrink-0" />
              History entry removed. The website is unchanged.
            </div>
          )}

          {Object.keys(results).length > 0 && (
            <div
              className={`flex items-center gap-2 px-4 py-3 rounded-lg border text-sm ${
                failedKeys.length || counts.blocked
                  ? "bg-amber-50 border-amber-200 text-amber-800"
                  : "bg-green-50 border-green-200 text-green-800"
              }`}
            >
              <CheckCircle2 className="w-4 h-4 shrink-0" />
              {counts.deleted || 0} deleted
              {failedKeys.length > 0 && `, ${failedKeys.length} failed part-way (retry resumes them)`}
              {counts.blocked > 0 && `, ${counts.blocked} not deleted`}.
            </div>
          )}

          {progress && busy && (
            <div className="flex flex-col gap-1.5">
              <div className="h-2 rounded-full bg-gray-100 overflow-hidden">
                <div
                  className="h-full bg-red-500 transition-all"
                  style={{ width: `${(100 * progress.done) / Math.max(progress.total, 1)}%` }}
                />
              </div>
              <span className="text-xs text-gray-500">
                {progress.done} / {progress.total} websites processed…
              </span>
            </div>
          )}

          {preview && (
            <>
              {!finished && (
                <p className="text-sm text-gray-700">
                  {scope === "record" ? (
                    "Only this entry is removed from the history. The website stays online."
                  ) : (
                    <>
                      <span className="font-semibold text-gray-900">{deletable.length}</span>{" "}
                      website{deletable.length !== 1 ? "s" : ""} will be permanently deleted:
                      every version, its files and its GCP resources. This cannot be undone.
                    </>
                  )}
                </p>
              )}

              {recordOnlyAllowed && !finished && (
                <fieldset className="flex flex-col gap-2 text-sm">
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="scope"
                      checked={scope === "website"}
                      onChange={() => setScope("website")}
                      className="mt-1 accent-red-600"
                    />
                    <span>
                      <span className="font-medium">Delete the website</span>
                      <span className="block text-xs text-gray-500">
                        Takes it offline and removes all {websites[0].versions} versions.
                      </span>
                    </span>
                  </label>
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="scope"
                      checked={scope === "record"}
                      onChange={() => setScope("record")}
                      className="mt-1 accent-red-600"
                    />
                    <span>
                      <span className="font-medium">Remove only this history entry</span>
                      <span className="block text-xs text-gray-500">
                        This is an older version; the live website is not touched.
                      </span>
                    </span>
                  </label>
                </fieldset>
              )}

              {scope === "website" &&
                preview.warnings?.map((w) => (
                  <div
                    key={w}
                    className="flex items-start gap-2 px-4 py-3 rounded-lg bg-amber-50
                      border border-amber-200 text-sm text-amber-800"
                  >
                    <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                    {w}
                  </div>
                ))}

              {scope === "website" && websites.length > 0 && (
                <ul className="border border-gray-200 rounded-lg px-4 divide-y divide-gray-100 max-h-72 overflow-y-auto">
                  {websites.map((w) => (
                    <WebsiteRow key={w.key} site={w} result={results[w.key]} />
                  ))}
                </ul>
              )}

              {needsTyping && !finished && (
                <label className="text-sm text-gray-700 flex flex-col gap-1.5">
                  <span>
                    Type <span className="font-mono font-semibold">DELETE</span> to confirm
                    {deletable.some((w) => w.mode === "prod") && " (production domains included)"}
                  </span>
                  <input
                    type="text"
                    value={confirmText}
                    onChange={(e) => setConfirmText(e.target.value)}
                    autoComplete="off"
                    className="px-3 py-2 border border-gray-200 rounded-lg text-sm
                      focus:outline-none focus:ring-2 focus:ring-red-500 focus:border-transparent"
                  />
                </label>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-200">
          <button
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 rounded-lg text-sm font-medium text-gray-700 bg-white
              border border-gray-200 hover:bg-gray-50 disabled:opacity-40
              disabled:cursor-not-allowed transition-colors"
          >
            {finished ? "Close" : "Cancel"}
          </button>
          {finished && failedKeys.length > 0 && (
            <button
              onClick={() => runWebsiteDeletes(failedKeys)}
              disabled={busy}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm
                font-medium text-white bg-red-600 hover:bg-red-700 disabled:opacity-40
                disabled:cursor-not-allowed transition-colors"
            >
              {busy ? <Loader className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
              Retry {failedKeys.length} failed
            </button>
          )}
          {!finished && (
            <button
              onClick={execute}
              disabled={!canRun}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm
                font-medium text-white bg-red-600 hover:bg-red-700 disabled:opacity-40
                disabled:cursor-not-allowed transition-colors"
            >
              {busy && preview ? (
                <Loader className="w-4 h-4 animate-spin" />
              ) : (
                <Trash2 className="w-4 h-4" />
              )}
              {scope === "record"
                ? "Remove entry"
                : `Delete ${deletable.length > 0 ? deletable.length : ""} website${deletable.length !== 1 ? "s" : ""}`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════════
   History table
   ═══════════════════════════════════════════════════════════════════════ */

/**
 * Deployment history table with date-range and mode filters, row selection,
 * single and bulk delete, a sortable date column, and clickable rows.
 */
export default function DeploymentHistory() {
  const navigate = useNavigate();
  const [deployments, setDeployments] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [sortAsc, setSortAsc] = useState(false); // default newest first
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [modes, setModes] = useState([]); // empty = every mode
  const [selected, setSelected] = useState(() => new Set());
  const [bulkRequest, setBulkRequest] = useState(null);

  const modesKey = modes.join(",");

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { deployments: rows, total: matching } = await getDeployments({
        startDate: fromDate,
        endDate: toDate,
        modes: modesKey ? modesKey.split(",") : [],
      });
      setDeployments(rows);
      setTotal(matching);
      // Drop selections that fell out of the new result set
      setSelected((prev) => {
        const ids = new Set(rows.map((r) => r.id));
        return new Set([...prev].filter((id) => ids.has(id)));
      });
    } catch (err) {
      setError(errorText(err, "Failed to load deployments"));
    } finally {
      setLoading(false);
    }
  }, [fromDate, toDate, modesKey]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleDelete = (e, dep) => {
    e.stopPropagation();
    setBulkRequest({
      description: `${dep.website_name} · ${MODE_LABELS[dep.mode] || dep.mode}`,
      payload: { ids: [dep.id], includeProd: true },
      single: dep,
    });
  };

  const sorted = useMemo(() => {
    const copy = [...deployments];
    copy.sort((a, b) => {
      const dateA = new Date(a.created_at || 0).getTime();
      const dateB = new Date(b.created_at || 0).getTime();
      return sortAsc ? dateA - dateB : dateB - dateA;
    });
    return copy;
  }, [deployments, sortAsc]);

  const formatDate = (dateStr) => {
    if (!dateStr) return "N/A";
    return new Date(dateStr).toLocaleString();
  };

  const filtered = Boolean(fromDate || toDate || modes.length);
  const truncated = total > deployments.length;

  const selectableIds = useMemo(
    () => sorted.filter((d) => DELETABLE.has(d.status)).map((d) => d.id),
    [sorted]
  );
  const allSelected =
    selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));
  const someSelected = selected.size > 0 && !allSelected;

  const headerCheckbox = useRef(null);
  useEffect(() => {
    if (headerCheckbox.current) headerCheckbox.current.indeterminate = someSelected;
  }, [someSelected]);

  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(selectableIds));
  };

  const toggleOne = (id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const applyPreset = (preset) => {
    const { from, to } = preset.range();
    setFromDate(from);
    setToDate(to);
  };

  const isActivePreset = (preset) => {
    const { from, to } = preset.range();
    return from === fromDate && to === toDate;
  };

  const toggleMode = (mode) => {
    setModes((prev) =>
      prev.includes(mode) ? prev.filter((m) => m !== mode) : [...prev, mode]
    );
  };

  const clearFilters = () => {
    setFromDate("");
    setToDate("");
    setModes([]);
  };

  // Keep the two inputs from crossing over
  const handleFrom = (value) => {
    setFromDate(value);
    if (value && toDate && value > toDate) setToDate(value);
  };

  const handleTo = (value) => {
    setToDate(value);
    if (value && fromDate && value < fromDate) setFromDate(value);
  };

  const rangeLabel = `${fromDate || "the start"} → ${toDate || "today"}`;

  const deleteSelected = () =>
    setBulkRequest({
      description: `${selected.size} selected deployment${
        selected.size !== 1 ? "s" : ""
      } → the websites they belong to`,
      payload: { ids: [...selected], includeProd: true },
    });

  const deleteMatching = () =>
    setBulkRequest({
      description: [
        modes.length ? modes.map((m) => MODE_LABELS[m] || m).join(" + ") : "All modes",
        fromDate || toDate ? rangeLabel : "all time",
      ].join(" · "),
      payload: {
        startDate: fromDate,
        endDate: toDate,
        modes,
        includeProd: modes.includes("prod"),
      },
    });

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Deployment History</h1>
          <p className="mt-1 text-gray-600">
            {loading
              ? "Loading…"
              : `${deployments.length} of ${total} deployment${total !== 1 ? "s" : ""}`}
            {filtered && !loading && (
              <span className="text-gray-400"> · {rangeLabel}</span>
            )}
          </p>
        </div>
        <button
          onClick={fetchData}
          className="inline-flex items-center gap-2 px-4 py-2 bg-white border border-gray-200
            rounded-lg text-sm font-medium text-gray-700 hover:bg-gray-50 transition-colors"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {/* ── Filters ───────────────────────────────────────────────────── */}
      <div
        className="bg-white rounded-xl border border-gray-200 px-4 py-3 mb-4
          flex flex-wrap items-center gap-x-4 gap-y-3"
      >
        <span className="inline-flex items-center gap-2 text-sm font-medium text-gray-700">
          <CalendarRange className="w-4 h-4 text-gray-400" />
          Date range
        </span>

        <div className="flex items-center gap-2">
          <label htmlFor="from-date" className="text-sm text-gray-500">
            From
          </label>
          <input
            id="from-date"
            type="date"
            value={fromDate}
            max={toDate || undefined}
            onChange={(e) => handleFrom(e.target.value)}
            className="px-2.5 py-1.5 border border-gray-200 rounded-lg text-sm text-gray-700
              focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent"
          />
          <label htmlFor="to-date" className="text-sm text-gray-500">
            To
          </label>
          <input
            id="to-date"
            type="date"
            value={toDate}
            min={fromDate || undefined}
            onChange={(e) => handleTo(e.target.value)}
            className="px-2.5 py-1.5 border border-gray-200 rounded-lg text-sm text-gray-700
              focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent"
          />
        </div>

        <div className="flex items-center gap-1.5">
          {PRESETS.map((preset) => (
            <button
              key={preset.label}
              onClick={() => applyPreset(preset)}
              className={`px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors
                ${
                  isActivePreset(preset)
                    ? "bg-[#2563EB] text-white"
                    : "bg-gray-50 text-gray-600 hover:bg-gray-100"
                }`}
            >
              {preset.label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2 border-l border-gray-200 pl-4">
          <span className="text-sm font-medium text-gray-700">Mode</span>
          {MODE_OPTIONS.map((mode) => (
            <button
              key={mode}
              onClick={() => toggleMode(mode)}
              aria-pressed={modes.includes(mode)}
              className={`px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors
                ${
                  modes.includes(mode)
                    ? "bg-[#2563EB] text-white"
                    : "bg-gray-50 text-gray-600 hover:bg-gray-100"
                }`}
            >
              {MODE_LABELS[mode]}
            </button>
          ))}
        </div>

        {filtered && (
          <button
            onClick={clearFilters}
            className="inline-flex items-center gap-1 text-xs font-medium text-gray-500
              hover:text-gray-700 transition-colors"
          >
            <X className="w-3.5 h-3.5" />
            Clear
          </button>
        )}
      </div>

      {/* ── Bulk actions ──────────────────────────────────────────────── */}
      {!loading && !error && sorted.length > 0 && (
        <div
          className="flex flex-wrap items-center gap-3 mb-4 px-4 py-3 rounded-xl
            border border-gray-200 bg-white"
        >
          <span className="text-sm text-gray-600">
            {selected.size > 0
              ? `${selected.size} selected`
              : "Select rows, or delete everything matching the filters"}
          </span>

          <div className="flex items-center gap-2 ml-auto">
            {selected.size > 0 && (
              <>
                <button
                  onClick={() => setSelected(new Set())}
                  className="px-3 py-1.5 rounded-lg text-xs font-medium text-gray-600
                    bg-gray-50 hover:bg-gray-100 transition-colors"
                >
                  Clear selection
                </button>
                <button
                  onClick={deleteSelected}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs
                    font-medium text-white bg-red-600 hover:bg-red-700 transition-colors"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  Delete {selected.size} selected
                </button>
              </>
            )}
            <button
              onClick={deleteMatching}
              disabled={!filtered}
              title={
                filtered
                  ? undefined
                  : "Set a date range or a mode first — deleting everything at once is not allowed"
              }
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs
                font-medium text-red-700 bg-red-50 hover:bg-red-100 disabled:opacity-40
                disabled:cursor-not-allowed transition-colors"
            >
              <Trash2 className="w-3.5 h-3.5" />
              Delete all matching
            </button>
          </div>
        </div>
      )}

      {truncated && !loading && (
        <div
          className="mb-4 px-4 py-3 rounded-lg bg-amber-50 border border-amber-200
            text-sm text-amber-800"
        >
          Showing the {deployments.length} most recent of {total} matching
          deployments. Narrow the date range to see the older ones.
        </div>
      )}

      {/* ── Results ───────────────────────────────────────────────────── */}
      {error ? (
        <div className="max-w-2xl mx-auto text-center py-16">
          <p className="text-red-600 font-medium mb-4">{error}</p>
          <button
            onClick={fetchData}
            className="inline-flex items-center gap-2 px-4 py-2 bg-[#2563EB] text-white
              rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Retry
          </button>
        </div>
      ) : loading ? (
        <div className="flex items-center justify-center py-24">
          <Loader className="w-8 h-8 text-[#2563EB] animate-spin" />
        </div>
      ) : sorted.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-24 text-gray-400">
          <Inbox className="w-16 h-16 mb-4" />
          <p className="text-lg font-medium text-gray-500">
            {filtered ? "No deployments match these filters" : "No deployments yet"}
          </p>
          {filtered ? (
            <button
              onClick={clearFilters}
              className="mt-3 text-sm font-medium text-[#2563EB] hover:text-blue-700
                transition-colors"
            >
              Show all deployments
            </button>
          ) : (
            <p className="text-sm text-gray-400 mt-1">
              Deploy your first website to see it here.
            </p>
          )}
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-200">
                  <th className="px-4 py-3 w-10">
                    <input
                      ref={headerCheckbox}
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      disabled={selectableIds.length === 0}
                      aria-label="Select all deletable deployments"
                      className="w-4 h-4 accent-[#2563EB] cursor-pointer disabled:cursor-not-allowed"
                    />
                  </th>
                  <th className="px-6 py-3 text-left font-semibold text-gray-600">
                    Website Name
                  </th>
                  <th className="px-6 py-3 text-left font-semibold text-gray-600">
                    Mode
                  </th>
                  <th className="px-6 py-3 text-left font-semibold text-gray-600">
                    Status
                  </th>
                  <th className="px-6 py-3 text-left font-semibold text-gray-600">
                    URL
                  </th>
                  <th
                    className="px-6 py-3 text-left font-semibold text-gray-600 cursor-pointer
                      select-none hover:text-[#2563EB] transition-colors"
                    onClick={() => setSortAsc((prev) => !prev)}
                  >
                    <span className="inline-flex items-center gap-1">
                      Date
                      <ArrowUpDown className="w-3.5 h-3.5" />
                    </span>
                  </th>
                  <th className="px-6 py-3 text-left font-semibold text-gray-600">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {sorted.map((dep) => (
                  <tr
                    key={dep.id}
                    onClick={() => navigate(`/deployments/${dep.id}`)}
                    className={`hover:bg-gray-50 cursor-pointer transition-colors ${
                      selected.has(dep.id) ? "bg-blue-50/60" : ""
                    }`}
                  >
                    <td className="px-4 py-4" onClick={(e) => e.stopPropagation()}>
                      {DELETABLE.has(dep.status) && (
                        <input
                          type="checkbox"
                          checked={selected.has(dep.id)}
                          onChange={() => toggleOne(dep.id)}
                          aria-label={`Select ${dep.website_name}`}
                          className="w-4 h-4 accent-[#2563EB] cursor-pointer"
                        />
                      )}
                    </td>
                    <td className="px-6 py-4 font-medium text-gray-900">
                      {dep.website_name}
                    </td>
                    <td className="px-6 py-4">
                      <span
                        className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs
                          font-medium ring-1 ring-inset capitalize
                          ${MODE_BADGE[dep.mode] || MODE_BADGE.demo}`}
                      >
                        {dep.mode}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      <span
                        className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs
                          font-medium ring-1 ring-inset capitalize
                          ${STATUS_BADGE[dep.status] || STATUS_BADGE.queued}`}
                      >
                        {dep.status}
                      </span>
                      {dep.deletion_status === "deleting" && (
                        <span className="ml-1.5 inline-flex items-center gap-1 text-xs text-gray-500">
                          <Loader className="w-3 h-3 animate-spin" />
                          deleting
                        </span>
                      )}
                      {dep.deletion_status === "delete_failed" && (
                        <span
                          className="ml-1.5 inline-flex items-center px-2 py-0.5 rounded-full text-xs
                            font-medium ring-1 ring-inset bg-red-50 text-red-700 ring-red-200"
                          title={dep.deletion_error || "Deletion failed part-way"}
                        >
                          delete failed
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      {dep.result_url ? (
                        <a
                          href={dep.result_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          className="inline-flex items-center gap-1 text-[#2563EB] hover:text-blue-700
                            font-medium transition-colors"
                        >
                          <ExternalLink className="w-3.5 h-3.5" />
                          <span className="truncate max-w-[200px]">{dep.result_url}</span>
                        </a>
                      ) : (
                        <span className="text-gray-400">-</span>
                      )}
                    </td>
                    <td className="px-6 py-4 text-gray-500 whitespace-nowrap">
                      {formatDate(dep.created_at)}
                    </td>
                    <td className="px-6 py-4">
                      <div className="flex items-center gap-2">
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            navigate(`/deployments/${dep.id}`);
                          }}
                          className="text-[#2563EB] hover:text-blue-700 text-xs font-medium
                            transition-colors"
                        >
                          View
                        </button>
                        {DELETABLE.has(dep.status) && (
                          <button
                            onClick={(e) => handleDelete(e, dep)}
                            className="inline-flex items-center gap-1 text-red-500 hover:text-red-700
                              text-xs font-medium transition-colors disabled:opacity-50
                              disabled:cursor-not-allowed"
                            title={
                              dep.deletion_status === "delete_failed"
                                ? `Retry: ${dep.deletion_error || "the last deletion failed part-way"}`
                                : `Delete website ${dep.website_name}`
                            }
                          >
                            {dep.deletion_status === "delete_failed" ? (
                              <RefreshCw className="w-3.5 h-3.5" />
                            ) : (
                              <Trash2 className="w-3.5 h-3.5" />
                            )}
                            {dep.deletion_status === "delete_failed" ? "Retry delete" : "Delete"}
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {bulkRequest && (
        <DeleteDialog
          request={bulkRequest}
          onClose={() => setBulkRequest(null)}
          onCompleted={() => {
            setSelected(new Set());
            fetchData();
          }}
        />
      )}
    </div>
  );
}
