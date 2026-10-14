import { useState, useCallback, useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Loader, Rocket, CheckCircle, AlertTriangle, XCircle, Info, Eye, Calendar, X, GitBranch } from "lucide-react";
import UploadZone from "./UploadZone";
import QuotaRequestModal from "./QuotaRequestModal";
import {
  deployWebsite, checkDomain, createPreview, deletePreview,
  deployFromGit, getGitPushEvents, getGitConnections, lookupWebsite,
} from "../services/api";
import { useAuth } from "../contexts/AuthContext";

/**
 * Full deploy page: UploadZone + deployment configuration form.
 */
export default function DeploymentForm() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { userProfile, isSimpleUser } = useAuth();

  // ── Git deployment context (when navigated from /git push event) ──
  const fromGitEventId = searchParams.get("from_git");
  const fromGitCommit = searchParams.get("commit");
  const [gitContext, setGitContext] = useState(null); // { event, connection }

  const [uploadData, setUploadData] = useState(null); // { type, files } | null
  const [mode, setMode] = useState("demo"); // UI mode: "demo" | "prod" | "subdomain" | "cloudrun"
  const [websiteName, setWebsiteName] = useState("");
  const [domain, setDomain] = useState("");
  const [parentDomain, setParentDomain] = useState("");
  const [subdomainLabel, setSubdomainLabel] = useState("");
  const [deployerFirstName, setDeployerFirstName] = useState("");
  const [deployerLastName, setDeployerLastName] = useState("");
  const [deployerEmail, setDeployerEmail] = useState("");
  const [aiEnabled, setAiEnabled] = useState(false);
  const [notificationEmails, setNotificationEmails] = useState("");
  const [domainStatus, setDomainStatus] = useState(null); // null | "checking" | "owned" | "available" | "unavailable"
  const [domainPrice, setDomainPrice] = useState(null); // "12.00 USD/an"
  const [domainPurchaseConfirmed, setDomainPurchaseConfirmed] = useState(false);
  const domainCheckTimer = useRef(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [quotaModalOpen, setQuotaModalOpen] = useState(false);
  const [quotaModalMessage, setQuotaModalMessage] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewData, setPreviewData] = useState(null); // { preview_id, url }
  const [scheduledAt, setScheduledAt] = useState("");
  const [showSchedule, setShowSchedule] = useState(false);

  // Auto-fill from user profile
  useEffect(() => {
    if (!userProfile) return;
    if (userProfile.email) {
      setDeployerEmail(userProfile.email);
      setNotificationEmails(userProfile.email);
    }
    if (userProfile.display_name) {
      const parts = userProfile.display_name.split(" ", 2);
      setDeployerFirstName(parts[0] || "");
      setDeployerLastName(parts[1] || "");
    }
  }, [userProfile]);

  // Force demo mode for simple users
  useEffect(() => {
    if (isSimpleUser) {
      setMode("demo");
    }
  }, [isSimpleUser]);

  // Load Git deployment context when navigated from /git with ?from_git=XXX
  useEffect(() => {
    if (!fromGitEventId) return;
    let cancelled = false;
    (async () => {
      try {
        const [events, connections] = await Promise.all([
          getGitPushEvents(),
          getGitConnections(),
        ]);
        const event = events.find((e) => e.id === fromGitEventId);
        if (!event) {
          setError("Push event introuvable — redeployment via Git impossible.");
          return;
        }
        const connection = connections.find((c) => c.id === event.connection_id);
        if (!connection) {
          setError("Connexion Git introuvable.");
          return;
        }
        if (!cancelled) {
          setGitContext({ event, connection });
          // Pre-fill website name from repo name (slugified)
          if (!websiteName) {
            const slug = connection.repo_name
              .toLowerCase()
              .replace(/[\s_]+/g, "-")
              .replace(/[^a-z0-9-]/g, "")
              .replace(/-+/g, "-")
              .replace(/^-|-$/g, "");
            setWebsiteName(slug);
          }
        }
      } catch (err) {
        setError("Echec du chargement du contexte Git: " + (err.message || ""));
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromGitEventId]);

  // Auto-populate notification emails when deployer email changes
  useEffect(() => {
    if (deployerEmail.trim()) {
      setNotificationEmails((prev) => {
        // Only auto-populate if field is empty or was previously auto-set
        if (!prev.trim() || prev.trim() === deployerEmail.trim()) {
          return deployerEmail.trim();
        }
        return prev;
      });
    }
  }, [deployerEmail]);

  // Compose the full FQDN in subdomain mode
  const composedSubdomainFqdn =
    mode === "subdomain" && subdomainLabel.trim() && parentDomain.trim()
      ? `${subdomainLabel.trim()}.${parentDomain.trim()}`
      : "";

  // The domain to validate depends on the mode
  const effectiveDomain =
    mode === "subdomain" ? composedSubdomainFqdn : domain.trim();

  // Debounced domain check when domain changes in prod or subdomain mode
  useEffect(() => {
    // Reset on mode/domain change
    setDomainStatus(null);
    setDomainPrice(null);
    setDomainPurchaseConfirmed(false);

    if (domainCheckTimer.current) {
      clearTimeout(domainCheckTimer.current);
    }

    if (mode !== "prod" && mode !== "subdomain") {
      return;
    }

    const toCheck = effectiveDomain;
    if (!toCheck || toCheck.length < 3 || !toCheck.includes(".")) {
      return;
    }

    setDomainStatus("checking");

    domainCheckTimer.current = setTimeout(async () => {
      try {
        const result = await checkDomain(toCheck);
        setDomainStatus(result.status);
        if (result.status === "available" && result.price_amount != null) {
          setDomainPrice(
            `${result.price_amount.toFixed(2)} ${result.price_currency || "USD"}/an`
          );
        }
      } catch {
        // API error — don't block deployment, treat as external
        setDomainStatus("external");
      }
    }, 800);

    return () => {
      if (domainCheckTimer.current) {
        clearTimeout(domainCheckTimer.current);
      }
    };
  }, [effectiveDomain, mode]);

  // ── Which website this deploy targets: new, or an update of an existing one ──
  const [siteInfo, setSiteInfo] = useState(null);
  const lookupBackendMode = mode === "subdomain" ? "prod" : mode;
  const lookupDomain = mode === "subdomain" ? composedSubdomainFqdn : mode === "prod" ? domain.trim() : "";
  useEffect(() => {
    setSiteInfo(null);
    const name = websiteName.trim();
    if (name.length < 2 || (lookupBackendMode === "prod" && !lookupDomain.includes("."))) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const info = await lookupWebsite({
          mode: lookupBackendMode, websiteName: name, domain: lookupDomain,
        });
        if (!cancelled) setSiteInfo(info);
      } catch {
        // Informational only — never block a deploy on it.
      }
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [websiteName, lookupBackendMode, lookupDomain]);

  // Auto-slugify website name: lowercase, replace spaces/underscores with hyphens,
  // remove non-alphanumeric characters (except hyphens), collapse multiple hyphens.
  const slugify = (value) =>
    value
      .toLowerCase()
      .replace(/[\s_]+/g, "-")
      .replace(/[^a-z0-9-]/g, "")
      .replace(/-+/g, "-")
      .replace(/^-|-$/g, "");

  const handleWebsiteNameChange = useCallback((e) => {
    setWebsiteName(slugify(e.target.value));
  }, []);

  // Domain is valid if: not prod/subdomain, or check pending/passed, or external.
  // "checking" is allowed so the deploy button isn't blocked while the API responds
  // — the backend validates domain ownership independently.
  const domainValid =
    (mode !== "prod" && mode !== "subdomain") ||
    domainStatus === null ||
    domainStatus === "checking" ||
    domainStatus === "owned" ||
    domainStatus === "external" ||
    (domainStatus === "available" && domainPurchaseConfirmed);

  // In git mode, no upload is required (source comes from the repo)
  const hasGitSource = Boolean(gitContext);
  const hasUpload = uploadData && uploadData.files.length > 0;

  const subdomainValid =
    mode !== "subdomain" ||
    (subdomainLabel.trim().length > 0 &&
      parentDomain.trim().length > 0 &&
      parentDomain.includes("."));

  const canSubmit =
    (hasUpload || hasGitSource) &&
    websiteName.trim().length > 0 &&
    deployerFirstName.trim().length > 0 &&
    deployerLastName.trim().length > 0 &&
    deployerEmail.trim().length > 0 &&
    (mode !== "prod" || domain.trim().length > 0) &&
    subdomainValid &&
    domainValid;

  // Preview handler
  const handlePreview = useCallback(async () => {
    if (!uploadData || previewLoading) return;
    setPreviewLoading(true);
    setError(null);

    try {
      const formData = new FormData();
      if (uploadData.type === "zip") {
        formData.append("zip_file", uploadData.files[0]);
      } else {
        for (const f of uploadData.files) {
          const filename = f.webkitRelativePath || f.name;
          formData.append("files", f, filename);
        }
      }

      const data = await createPreview(formData);
      const baseUrl = import.meta.env.VITE_API_URL || window.location.origin;
      setPreviewData({
        preview_id: data.preview_id,
        url: `${baseUrl}${data.url}`,
        ttl: data.ttl_seconds,
      });
    } catch (err) {
      setError("Preview failed: " + (err.response?.data?.detail || err.message));
    } finally {
      setPreviewLoading(false);
    }
  }, [uploadData, previewLoading]);

  const handleClosePreview = useCallback(async () => {
    if (previewData?.preview_id) {
      try {
        await deletePreview(previewData.preview_id);
      } catch {}
    }
    setPreviewData(null);
  }, [previewData]);

  const handleSubmit = useCallback(
    async (e) => {
      e.preventDefault();
      if (!canSubmit || submitting) return;

      setSubmitting(true);
      setError(null);

      // Subdomain UI mode is translated to backend mode=prod with composed domain.
      // prod_deployer detects the parent Cloud DNS zone and adds an A record.
      const backendMode = mode === "subdomain" ? "prod" : mode;
      const backendDomain =
        mode === "subdomain"
          ? composedSubdomainFqdn
          : mode === "prod"
            ? domain.trim()
            : "";

      try {
        // ── Git deployment path ──────────────────────────────────────
        if (gitContext) {
          const body = {
            connection_id: gitContext.connection.id,
            commit_sha: fromGitCommit || gitContext.event.commit_sha,
            push_event_id: gitContext.event.id,
            mode: backendMode,
            website_name: websiteName.trim(),
            deployer_first_name: deployerFirstName.trim(),
            deployer_last_name: deployerLastName.trim(),
            deployer_email: deployerEmail.trim(),
            ai_enabled: aiEnabled,
            domain_purchase_confirmed: domainPurchaseConfirmed,
            notification_emails: notificationEmails.trim() || "",
          };
          if (backendDomain) {
            body.domain = backendDomain;
          }
          const data = await deployFromGit(body);
          navigate(`/deployments/${data.deployment_id}`);
          return;
        }

        // ── Upload-based deployment path ────────────────────────────
        const formData = new FormData();

        if (uploadData.type === "zip") {
          formData.append("zip_file", uploadData.files[0]);
        } else {
          for (const f of uploadData.files) {
            const filename = f.webkitRelativePath || f.name;
            formData.append("files", f, filename);
          }
        }

        formData.append("mode", backendMode);
        formData.append("website_name", websiteName.trim());
        formData.append("deployer_first_name", deployerFirstName.trim());
        formData.append("deployer_last_name", deployerLastName.trim());
        formData.append("deployer_email", deployerEmail.trim());
        formData.append("ai_enabled", aiEnabled ? "true" : "false");

        if (backendDomain) {
          formData.append("domain", backendDomain);
          if (domainPurchaseConfirmed) {
            formData.append("domain_purchase_confirmed", "true");
          }
        }

        if (notificationEmails.trim()) {
          formData.append("notification_emails", notificationEmails.trim());
        }

        // Scheduled deployment
        if (showSchedule && scheduledAt) {
          formData.append("scheduled_at", new Date(scheduledAt).toISOString());
        }

        const data = await deployWebsite(formData);
        navigate(`/deployments/${data.deployment_id}`);
      } catch (err) {
        const detail =
          err.response?.data?.detail ||
          err.message ||
          "Deployment failed. Please try again.";
        const detailStr = typeof detail === "string" ? detail : JSON.stringify(detail);
        setError(detailStr);
        // If the backend returned a quota error (HTTP 429), open the request modal
        if (err.response?.status === 429) {
          setQuotaModalMessage(detailStr);
          setQuotaModalOpen(true);
        }
      } finally {
        setSubmitting(false);
      }
    },
    [canSubmit, submitting, uploadData, mode, websiteName, domain, parentDomain, subdomainLabel, composedSubdomainFqdn, deployerFirstName, deployerLastName, deployerEmail, aiEnabled, notificationEmails, domainPurchaseConfirmed, showSchedule, scheduledAt, navigate, gitContext, fromGitCommit]
  );

  return (
    <div className="max-w-2xl mx-auto">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900">Deploy Website</h1>
        <p className="mt-2 text-gray-600">
          {gitContext
            ? "Deploiement depuis Git — pas besoin d'upload."
            : "Upload your website files and configure the deployment."}
        </p>
      </div>

      {/* Git context banner */}
      {gitContext && (
        <div className="mb-6 p-4 bg-gray-900 text-white rounded-lg border-2 border-gray-800">
          <div className="flex items-start gap-3">
            <GitBranch className="w-5 h-5 text-emerald-400 flex-shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-semibold">
                {gitContext.connection.provider === "github" ? "GitHub" : "GitLab"} &mdash; {gitContext.connection.repo_name}
              </div>
              <div className="mt-1 text-xs text-gray-300 font-mono truncate">
                {gitContext.event.branch} @ {gitContext.event.commit_sha.substring(0, 12)}
              </div>
              {gitContext.event.commit_message && (
                <div className="mt-1 text-xs text-gray-400 truncate">
                  {gitContext.event.commit_message}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Upload Zone (hidden in git mode) */}
      {!gitContext && <UploadZone onFilesSelected={setUploadData} />}

      {/* Configuration form */}
      {(uploadData || gitContext) && (
        <form onSubmit={handleSubmit} className="mt-8 space-y-6">
          {/* Deployer identity fields */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="deployer-first-name"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Pr&eacute;nom <span className="text-red-500">*</span>
              </label>
              <input
                id="deployer-first-name"
                type="text"
                value={deployerFirstName}
                onChange={(e) => setDeployerFirstName(e.target.value)}
                placeholder="Jean"
                className="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm
                  focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent
                  placeholder-gray-400"
                required
              />
            </div>
            <div>
              <label
                htmlFor="deployer-last-name"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Nom <span className="text-red-500">*</span>
              </label>
              <input
                id="deployer-last-name"
                type="text"
                value={deployerLastName}
                onChange={(e) => setDeployerLastName(e.target.value)}
                placeholder="Dupont"
                className="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm
                  focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent
                  placeholder-gray-400"
                required
              />
            </div>
          </div>

          <div>
            <label
              htmlFor="deployer-email"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Email <span className="text-red-500">*</span>
            </label>
            <input
              id="deployer-email"
              type="email"
              value={deployerEmail}
              onChange={(e) => setDeployerEmail(e.target.value)}
              placeholder="jean.dupont@example.com"
              readOnly={!!userProfile?.email}
              className={`w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm
                focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent
                placeholder-gray-400 ${userProfile?.email ? "bg-gray-100 text-gray-500 cursor-not-allowed" : ""}`}
              required
            />
          </div>

          {/* Mode selector */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Deployment Mode
            </label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setMode("demo")}
                className={`flex-1 py-3 px-4 rounded-lg text-sm font-semibold border-2 transition-all ${
                  mode === "demo"
                    ? "border-[#2563EB] bg-[#2563EB] text-white shadow-md"
                    : "border-gray-200 bg-white text-gray-700 hover:border-gray-300"
                }`}
              >
                Demo
                <span className="block text-xs font-normal mt-0.5 opacity-80">
                  Path-based preview
                </span>
              </button>
              {!isSimpleUser && (
                <>
                  <button
                    type="button"
                    onClick={() => setMode("prod")}
                    className={`flex-1 py-3 px-4 rounded-lg text-sm font-semibold border-2 transition-all ${
                      mode === "prod"
                        ? "border-emerald-600 bg-emerald-600 text-white shadow-md"
                        : "border-gray-200 bg-white text-gray-700 hover:border-gray-300"
                    }`}
                  >
                    Production
                    <span className="block text-xs font-normal mt-0.5 opacity-80">
                      Custom domain
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setMode("subdomain")}
                    className={`flex-1 py-3 px-4 rounded-lg text-sm font-semibold border-2 transition-all ${
                      mode === "subdomain"
                        ? "border-indigo-600 bg-indigo-600 text-white shadow-md"
                        : "border-gray-200 bg-white text-gray-700 hover:border-gray-300"
                    }`}
                  >
                    Subdomain
                    <span className="block text-xs font-normal mt-0.5 opacity-80">
                      Zone DNS existante
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setMode("cloudrun")}
                    className={`flex-1 py-3 px-4 rounded-lg text-sm font-semibold border-2 transition-all ${
                      mode === "cloudrun"
                        ? "border-teal-600 bg-teal-600 text-white shadow-md"
                        : "border-gray-200 bg-white text-gray-700 hover:border-gray-300"
                    }`}
                  >
                    Cloud Run
                    <span className="block text-xs font-normal mt-0.5 opacity-80">
                      Deploy any app
                    </span>
                  </button>
                </>
              )}
            </div>
          </div>

          {/* Website name */}
          <div>
            <label
              htmlFor="website-name"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Website Name <span className="text-red-500">*</span>
            </label>
            <input
              id="website-name"
              type="text"
              value={websiteName}
              onChange={handleWebsiteNameChange}
              placeholder="my-awesome-site"
              className="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm
                focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent
                placeholder-gray-400"
              required
            />
            <p className="mt-1 text-xs text-gray-500">
              Auto-slugified: lowercase, hyphens only.
            </p>
            {siteInfo && !siteInfo.exists && siteInfo.url && (
              <p className="mt-1.5 text-xs text-blue-600 font-medium">
                New website — URL: {siteInfo.url}
              </p>
            )}
            {siteInfo?.exists && siteInfo.status && siteInfo.status !== "active" && (
              <p className="mt-1.5 text-xs text-red-600 font-medium">
                This website is being deleted or its deletion failed — finish the
                deletion from the history page before deploying it again.
              </p>
            )}
            {siteInfo?.exists && (!siteInfo.status || siteInfo.status === "active") && (
              <p className="mt-1.5 text-xs text-amber-700 font-medium">
                {siteInfo.owned_by_other
                  ? "Another user's website already uses this name — deploying replaces its content."
                  : `Existing website (${siteInfo.versions} version${siteInfo.versions !== 1 ? "s" : ""}) — this deploy replaces its content.`}
                {siteInfo.url && <> URL: {siteInfo.url}</>}
              </p>
            )}
          </div>

          {/* Domain (prod mode only) */}
          {mode === "prod" && (
            <div>
              <label
                htmlFor="domain"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Domain <span className="text-red-500">*</span>
              </label>
              <input
                id="domain"
                type="text"
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                placeholder="client-site.com ou blog.client-site.com"
                className="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm
                  focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent
                  placeholder-gray-400"
                required
              />
              <p className="mt-1 text-xs text-gray-500">
                Domaine racine (example.com) ou sous-domaine (blog.example.com).
              </p>

              {/* Domain status indicator */}
              {domainStatus === "checking" && (
                <div className="mt-2 flex items-center gap-2 text-sm text-gray-500">
                  <Loader className="w-4 h-4 animate-spin" />
                  V&eacute;rification du domaine...
                </div>
              )}
              {domainStatus === "owned" && (
                <div className="mt-2 flex items-center gap-2 text-sm text-emerald-600">
                  <CheckCircle className="w-4 h-4" />
                  Domaine d&eacute;j&agrave; enregistr&eacute; dans le projet GCP
                </div>
              )}
              {domainStatus === "available" && (
                <div className="mt-2 space-y-2">
                  <div className="flex items-center gap-2 text-sm text-amber-600">
                    <AlertTriangle className="w-4 h-4" />
                    Attention, on va acheter ce domaine &mdash; {domainPrice}
                  </div>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={domainPurchaseConfirmed}
                      onChange={(e) => setDomainPurchaseConfirmed(e.target.checked)}
                      className="h-4 w-4 rounded border-gray-300 text-amber-600 focus:ring-amber-500"
                    />
                    <span className="text-sm text-gray-700">
                      Je confirme l&apos;achat de ce domaine
                    </span>
                  </label>
                </div>
              )}
              {domainStatus === "external" && (
                <div className="mt-2 p-3 bg-blue-50 border border-blue-200 rounded-lg">
                  <div className="flex items-center gap-2 text-sm text-blue-700">
                    <Info className="w-4 h-4 flex-shrink-0" />
                    Domaine enregistr&eacute; chez un registrar externe (GoDaddy, OVH, etc.)
                  </div>
                  <p className="mt-1.5 text-xs text-blue-600 ml-6">
                    Apr&egrave;s le d&eacute;ploiement, allez dans votre registrar et changez les
                    nameservers pour pointer vers Google Cloud DNS. Les nameservers
                    seront affich&eacute;s dans les logs du d&eacute;ploiement.
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Prod mode — what the platform will provision */}
          {mode === "prod" && (
            <div className="p-4 bg-emerald-50 border border-emerald-200 rounded-lg">
              <h4 className="text-sm font-semibold text-emerald-800 mb-2">
                Ressources provisionnees automatiquement
              </h4>
              <ul className="space-y-1.5 text-xs text-emerald-700">
                <li className="flex items-start gap-2">
                  <CheckCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                  <span><strong>Bucket GCS</strong> + Backend Bucket avec <strong>CDN</strong> active</span>
                </li>
                <li className="flex items-start gap-2">
                  <CheckCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                  <span><strong>Certificat SSL</strong> Google-managed (provisionnement auto, jusqu&apos;a 24h)</span>
                </li>
                <li className="flex items-start gap-2">
                  <CheckCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                  <span><strong>Zone DNS</strong> Cloud DNS + records <code className="bg-emerald-100 px-1 rounded">A</code> et <code className="bg-emerald-100 px-1 rounded">CNAME (www)</code></span>
                </li>
                <li className="flex items-start gap-2">
                  <CheckCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                  <span>Ajout au <strong>Load Balancer</strong> partage (host rule + path matcher)</span>
                </li>
              </ul>
              <p className="mt-2.5 text-[11px] text-emerald-600">
                Apres le deploiement, les nameservers Google Cloud DNS seront affiches
                pour configuration chez votre registrar.
              </p>
            </div>
          )}

          {/* Subdomain mode fields */}
          {mode === "subdomain" && (
            <div className="space-y-3">
              <div className="grid grid-cols-5 gap-3 items-end">
                <div className="col-span-2">
                  <label
                    htmlFor="subdomain-label"
                    className="block text-sm font-medium text-gray-700 mb-1"
                  >
                    Sous-domaine <span className="text-red-500">*</span>
                  </label>
                  <input
                    id="subdomain-label"
                    type="text"
                    value={subdomainLabel}
                    onChange={(e) => setSubdomainLabel(slugify(e.target.value))}
                    placeholder="blog"
                    className="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm
                      focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent
                      placeholder-gray-400"
                    required
                  />
                </div>
                <div className="flex items-center justify-center pb-3 text-gray-400 font-medium">.</div>
                <div className="col-span-2">
                  <label
                    htmlFor="parent-domain"
                    className="block text-sm font-medium text-gray-700 mb-1"
                  >
                    Domaine parent <span className="text-red-500">*</span>
                  </label>
                  <input
                    id="parent-domain"
                    type="text"
                    value={parentDomain}
                    onChange={(e) => setParentDomain(e.target.value.trim().toLowerCase())}
                    placeholder="example.com"
                    className="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm
                      focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent
                      placeholder-gray-400"
                    required
                  />
                </div>
              </div>

              <p className="text-xs text-gray-500">
                Le domaine parent doit d&eacute;j&agrave; avoir une zone DNS dans Cloud DNS / Cloud Domains.
                Un enregistrement <span className="font-mono">A</span> sera ajout&eacute; automatiquement
                pour <span className="font-mono">{composedSubdomainFqdn || "sousdomaine.domaine.com"}</span>.
              </p>

              {composedSubdomainFqdn && (
                <div className="p-3 bg-indigo-50 border border-indigo-200 rounded-lg">
                  <div className="text-sm text-indigo-700">
                    URL cible : <span className="font-mono font-semibold">https://{composedSubdomainFqdn}/</span>
                  </div>
                </div>
              )}

              {/* Status indicator for parent-zone existence */}
              {domainStatus === "checking" && (
                <div className="flex items-center gap-2 text-sm text-gray-500">
                  <Loader className="w-4 h-4 animate-spin" />
                  V&eacute;rification de la zone DNS parente...
                </div>
              )}
              {domainStatus === "owned" && (
                <div className="flex items-center gap-2 text-sm text-emerald-600">
                  <CheckCircle className="w-4 h-4" />
                  Zone DNS de <span className="font-mono">{parentDomain}</span> trouv&eacute;e &mdash; l&apos;enregistrement A sera ajout&eacute; automatiquement.
                </div>
              )}
              {domainStatus === "external" && (
                <div className="flex items-center gap-2 text-sm text-amber-600">
                  <AlertTriangle className="w-4 h-4" />
                  Aucune zone DNS trouv&eacute;e pour <span className="font-mono">{parentDomain}</span> dans ce projet GCP.
                  Le d&eacute;ploiement cr&eacute;era une zone d&eacute;di&eacute;e (nameservers &agrave; configurer chez le registrar).
                </div>
              )}
              {domainStatus === "available" && (
                <div className="flex items-center gap-2 text-sm text-amber-600">
                  <AlertTriangle className="w-4 h-4" />
                  Le domaine parent <span className="font-mono">{parentDomain}</span> n&apos;est pas enregistr&eacute; &mdash;
                  utilisez plut&ocirc;t le mode Production pour l&apos;acheter.
                </div>
              )}
            </div>
          )}

          {/* AI Validation toggle */}
          <div className="flex items-start gap-4 p-4 bg-gray-50 rounded-lg border border-gray-200">
            <div className="flex-1">
              <label
                htmlFor="ai-toggle"
                className="block text-sm font-medium text-gray-700"
              >
                AI Validation
              </label>
              <p className="mt-1 text-xs text-gray-500">
                Par d&eacute;faut d&eacute;sactiv&eacute;. En activant l&apos;IA, vous acceptez la
                responsabilit&eacute; de son utilisation et les co&ucirc;ts associ&eacute;s.
              </p>
            </div>
            <button
              id="ai-toggle"
              type="button"
              role="switch"
              aria-checked={aiEnabled}
              onClick={() => setAiEnabled((prev) => !prev)}
              className={`relative inline-flex h-7 w-12 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:ring-offset-2 ${
                aiEnabled ? "bg-[#2563EB]" : "bg-gray-300"
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-6 w-6 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                  aiEnabled ? "translate-x-5" : "translate-x-0"
                }`}
              />
            </button>
          </div>

          {/* Notification emails */}
          <div>
            <label
              htmlFor="emails"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Notification Emails
            </label>
            <input
              id="emails"
              type="text"
              value={notificationEmails}
              onChange={(e) => setNotificationEmails(e.target.value)}
              placeholder="alice@example.com, bob@example.com"
              className="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm
                focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent
                placeholder-gray-400"
            />
            <p className="mt-1 text-xs text-gray-500">
              Comma-separated email addresses to notify on completion.
            </p>
          </div>

          {/* Schedule toggle (super_user / admin only) */}
          {!isSimpleUser && (
            <div className="flex items-start gap-4 p-4 bg-gray-50 rounded-lg border border-gray-200">
              <div className="flex-1">
                <label className="block text-sm font-medium text-gray-700">
                  Planifier le deploiement
                </label>
                <p className="mt-1 text-xs text-gray-500">
                  Programmer le deploiement a une date et heure precise.
                </p>
                {showSchedule && (
                  <input
                    type="datetime-local"
                    value={scheduledAt}
                    onChange={(e) => setScheduledAt(e.target.value)}
                    min={new Date().toISOString().slice(0, 16)}
                    className="mt-2 w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm
                      focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-transparent"
                  />
                )}
              </div>
              <button
                type="button"
                onClick={() => { setShowSchedule((prev) => !prev); if (showSchedule) setScheduledAt(""); }}
                className={`relative inline-flex h-7 w-12 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:ring-offset-2 ${
                  showSchedule ? "bg-[#2563EB]" : "bg-gray-300"
                }`}
              >
                <span
                  className={`pointer-events-none inline-block h-6 w-6 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                    showSchedule ? "translate-x-5" : "translate-x-0"
                  }`}
                />
              </button>
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-lg">
              <p className="text-sm text-red-700">{error}</p>
              {/Quota/i.test(error) && (
                <button
                  type="button"
                  onClick={() => {
                    setQuotaModalMessage(error);
                    setQuotaModalOpen(true);
                  }}
                  className="mt-2 text-xs font-medium text-indigo-700 hover:text-indigo-900 underline cursor-pointer"
                >
                  Demander une augmentation de quota
                </button>
              )}
            </div>
          )}

          {/* Action buttons */}
          <div className="flex gap-3">
            {/* Preview button */}
            <button
              type="button"
              onClick={handlePreview}
              disabled={!uploadData || uploadData.files.length === 0 || previewLoading}
              className={`flex items-center justify-center gap-2 py-3 px-6 rounded-lg
                text-sm font-semibold transition-all border-2
                ${
                  uploadData && uploadData.files.length > 0 && !previewLoading
                    ? "border-gray-300 bg-white text-gray-700 hover:bg-gray-50 shadow-sm cursor-pointer"
                    : "border-gray-200 bg-gray-100 text-gray-400 cursor-not-allowed"
                }`}
            >
              {previewLoading ? (
                <Loader className="w-4 h-4 animate-spin" />
              ) : (
                <Eye className="w-4 h-4" />
              )}
              Preview
            </button>

            {/* Submit */}
            <button
              type="submit"
              disabled={!canSubmit || submitting}
              className={`flex-1 flex items-center justify-center gap-2 py-3 px-6 rounded-lg
                text-base font-semibold text-white transition-all
                ${
                  canSubmit && !submitting
                    ? "bg-[#2563EB] hover:bg-blue-700 shadow-md hover:shadow-lg cursor-pointer"
                    : "bg-gray-300 cursor-not-allowed"
                }`}
            >
              {submitting ? (
                <>
                  <Loader className="w-5 h-5 animate-spin" />
                  {showSchedule && scheduledAt ? "Scheduling..." : "Deploying..."}
                </>
              ) : showSchedule && scheduledAt ? (
                <>
                  <Calendar className="w-5 h-5" />
                  Planifier
                </>
              ) : gitContext ? (
                <>
                  <GitBranch className="w-5 h-5" />
                  Deploy from Git
                </>
              ) : (
                <>
                  <Rocket className="w-5 h-5" />
                  Deploy
                </>
              )}
            </button>
          </div>
        </form>
      )}

      <QuotaRequestModal
        open={quotaModalOpen}
        onClose={() => setQuotaModalOpen(false)}
        triggerMessage={quotaModalMessage}
      />

      {/* Preview Modal */}
      {previewData && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="bg-white rounded-xl shadow-2xl w-[90vw] h-[85vh] flex flex-col">
            <div className="flex items-center justify-between px-6 py-3 border-b border-gray-200">
              <div className="flex items-center gap-3">
                <Eye className="w-5 h-5 text-blue-600" />
                <span className="font-semibold text-gray-900">Preview</span>
                <span className="text-xs text-gray-400">
                  Expire dans {Math.floor(previewData.ttl / 60)} min
                </span>
              </div>
              <div className="flex items-center gap-2">
                <a
                  href={previewData.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-600 hover:underline"
                >
                  Ouvrir dans un nouvel onglet
                </a>
                <button
                  onClick={handleClosePreview}
                  className="p-1.5 rounded-md hover:bg-gray-100 cursor-pointer"
                >
                  <X className="w-5 h-5 text-gray-500" />
                </button>
              </div>
            </div>
            <iframe
              src={previewData.url}
              className="flex-1 w-full border-0"
              title="Site Preview"
            />
          </div>
        </div>
      )}
    </div>
  );
}
