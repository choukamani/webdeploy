import { useState } from "react";
import {
  CheckCircle,
  XCircle,
  ExternalLink,
  Globe,
  Clock,
  Tag,
  Copy,
  Check,
  Server,
  Shield,
  Database,
  Zap,
  Link2,
} from "lucide-react";

const RESOURCE_LABELS = {
  gcs_prefix: "Files",
  gcs_bucket: "Storage bucket",
  backend_bucket: "Backend bucket (CDN)",
  backend_service: "Backend service",
  serverless_neg: "Serverless NEG",
  cloud_run_service: "Cloud Run service",
  host_rule: "Host rule",
  path_rule: "Path rule",
  certificate: "Certificate",
  dns: "DNS record",
};

/**
 * Deployment result banner: shown when deployment reaches a terminal state.
 *
 * @param {{ deployment: object }} props
 *   deployment: { id, status, website_name, mode, url, domain, steps, ai_summary,
 *                 error_message, created_at, completed_at, dns_nameservers }
 */
export default function DeploymentStatus({ deployment }) {
  const [copiedNs, setCopiedNs] = useState(null);

  if (!deployment) return null;

  const { status } = deployment;
  const isSuccess = status === "success";
  const isFailed = status === "failed";

  if (!isSuccess && !isFailed) return null;

  // Find the step that failed
  const failedStep = deployment.steps?.find((s) => s.status === "failed");

  const formatDate = (dateStr) => {
    if (!dateStr) return "N/A";
    return new Date(dateStr).toLocaleString();
  };

  const handleCopyNs = async (ns, idx) => {
    try {
      await navigator.clipboard.writeText(ns);
      setCopiedNs(idx);
      setTimeout(() => setCopiedNs(null), 2000);
    } catch {
      // Fallback for non-HTTPS
    }
  };

  const handleCopyAll = async () => {
    try {
      const all = deployment.dns_nameservers.join("\n");
      await navigator.clipboard.writeText(all);
      setCopiedNs("all");
      setTimeout(() => setCopiedNs(null), 2000);
    } catch {
      // Fallback for non-HTTPS
    }
  };

  const showDnsInstructions =
    isSuccess &&
    deployment.mode === "prod" &&
    deployment.dns_nameservers &&
    deployment.dns_nameservers.length > 0;

  // Subdomain deployed into existing parent zone — no nameserver config needed
  const isSubdomain =
    isSuccess &&
    deployment.mode === "prod" &&
    deployment.domain &&
    deployment.domain.split(".").length > 2 &&
    (!deployment.dns_nameservers || deployment.dns_nameservers.length === 0);

  return (
    <div className="space-y-4">
      {/* Result banner */}
      <div
        className={`rounded-xl border-2 p-6 ${
          isSuccess
            ? "border-green-200 bg-green-50"
            : "border-red-200 bg-red-50"
        }`}
      >
        <div className="flex items-start gap-4">
          {isSuccess ? (
            <CheckCircle className="w-8 h-8 text-[#16a34a] flex-shrink-0 mt-0.5" />
          ) : (
            <XCircle className="w-8 h-8 text-[#dc2626] flex-shrink-0 mt-0.5" />
          )}

          <div className="flex-1 min-w-0">
            <h2
              className={`text-xl font-bold ${
                isSuccess ? "text-green-800" : "text-red-800"
              }`}
            >
              {isSuccess ? "Deployment Successful!" : "Deployment Failed"}
            </h2>

            {/* Success: show URL */}
            {isSuccess && deployment.url && (
              <a
                href={deployment.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 mt-2 text-[#2563EB] hover:text-blue-700
                  font-medium text-sm transition-colors"
              >
                <ExternalLink className="w-4 h-4" />
                {deployment.url}
              </a>
            )}

            {/* Failure: show which step failed */}
            {isFailed && failedStep && (
              <p className="mt-2 text-sm text-red-700">
                Failed at step:{" "}
                <span className="font-semibold">{failedStep.name}</span>
              </p>
            )}

            {/* Error message */}
            {isFailed && (deployment.error_message || failedStep?.error) && (
              <div className="mt-3 p-3 bg-red-100 rounded-lg border border-red-200">
                <p className="text-sm text-red-800 font-mono whitespace-pre-wrap">
                  {deployment.error_message || failedStep?.error}
                </p>
              </div>
            )}

            {/* AI Summary */}
            {isSuccess && deployment.ai_summary && (
              <div className="mt-4 p-4 bg-white rounded-lg border border-green-200">
                <h3 className="text-sm font-semibold text-gray-700 mb-1">
                  AI Summary
                </h3>
                <p className="text-sm text-gray-600 whitespace-pre-wrap">
                  {deployment.ai_summary}
                </p>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* DNS Configuration Instructions (prod mode with nameservers) */}
      {showDnsInstructions && (
        <div className="bg-white rounded-xl border-2 border-blue-200 overflow-hidden">
          {/* Header */}
          <div className="bg-blue-50 px-6 py-4 border-b border-blue-200">
            <div className="flex items-center gap-3">
              <Server className="w-5 h-5 text-blue-600" />
              <h3 className="text-lg font-bold text-blue-900">
                Configuration DNS requise
              </h3>
            </div>
            <p className="mt-1 text-sm text-blue-700">
              Pour que le domaine <strong>{deployment.domain}</strong> pointe
              vers votre site, configurez les nameservers chez votre registrar.
            </p>
          </div>

          <div className="p-6 space-y-6">
            {/* Nameservers table */}
            <div>
              <div className="flex items-center justify-between mb-3">
                <h4 className="text-sm font-semibold text-gray-800">
                  Nameservers Google Cloud DNS
                </h4>
                <button
                  onClick={handleCopyAll}
                  className="inline-flex items-center gap-1.5 text-xs text-blue-600 hover:text-blue-800
                    font-medium transition-colors"
                >
                  {copiedNs === "all" ? (
                    <>
                      <Check className="w-3.5 h-3.5" />
                      Copi&eacute; !
                    </>
                  ) : (
                    <>
                      <Copy className="w-3.5 h-3.5" />
                      Tout copier
                    </>
                  )}
                </button>
              </div>

              <div className="border border-gray-200 rounded-lg overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="bg-gray-50 border-b border-gray-200">
                      <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                        #
                      </th>
                      <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                        Nameserver
                      </th>
                      <th className="px-4 py-2.5 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">
                        Copier
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {deployment.dns_nameservers.map((ns, idx) => (
                      <tr
                        key={ns}
                        className={`${
                          idx % 2 === 0 ? "bg-white" : "bg-gray-50"
                        } border-b border-gray-100 last:border-b-0`}
                      >
                        <td className="px-4 py-3 text-sm text-gray-400 font-mono">
                          {idx + 1}
                        </td>
                        <td className="px-4 py-3 text-sm font-mono font-medium text-gray-900">
                          {ns}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => handleCopyNs(ns, idx)}
                            className="inline-flex items-center gap-1 text-xs text-gray-400
                              hover:text-blue-600 transition-colors"
                            title={`Copier ${ns}`}
                          >
                            {copiedNs === idx ? (
                              <Check className="w-4 h-4 text-green-500" />
                            ) : (
                              <Copy className="w-4 h-4" />
                            )}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Step-by-step instructions */}
            <div>
              <h4 className="text-sm font-semibold text-gray-800 mb-3">
                &Eacute;tapes &agrave; suivre
              </h4>
              <ol className="space-y-3">
                <li className="flex gap-3">
                  <span className="flex-shrink-0 w-7 h-7 rounded-full bg-blue-100 text-blue-700
                    text-sm font-bold flex items-center justify-center">
                    1
                  </span>
                  <div>
                    <p className="text-sm font-medium text-gray-800">
                      Connectez-vous &agrave; votre registrar
                    </p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      Allez sur le site de votre registrar (GoDaddy, OVH, Namecheap, etc.)
                      et connectez-vous &agrave; votre compte.
                    </p>
                  </div>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 w-7 h-7 rounded-full bg-blue-100 text-blue-700
                    text-sm font-bold flex items-center justify-center">
                    2
                  </span>
                  <div>
                    <p className="text-sm font-medium text-gray-800">
                      Trouvez les param&egrave;tres DNS du domaine
                    </p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      Allez dans <strong>Domain Settings</strong> &rarr; <strong>DNS</strong> &rarr; <strong>Nameservers</strong>.
                      Sur GoDaddy : My Domains &rarr; {deployment.domain} &rarr; DNS &rarr; Nameservers.
                    </p>
                  </div>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 w-7 h-7 rounded-full bg-blue-100 text-blue-700
                    text-sm font-bold flex items-center justify-center">
                    3
                  </span>
                  <div>
                    <p className="text-sm font-medium text-gray-800">
                      Changez les nameservers en &laquo; Custom &raquo;
                    </p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      S&eacute;lectionnez <strong>&laquo; Custom Nameservers &raquo;</strong> (ou &laquo; Personnalis&eacute; &raquo;)
                      et remplacez les nameservers existants par les {deployment.dns_nameservers.length} nameservers
                      Google ci-dessus.
                    </p>
                  </div>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 w-7 h-7 rounded-full bg-blue-100 text-blue-700
                    text-sm font-bold flex items-center justify-center">
                    4
                  </span>
                  <div>
                    <p className="text-sm font-medium text-gray-800">
                      Sauvegardez et patientez
                    </p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      Cliquez sur <strong>Save</strong>. La propagation DNS peut prendre
                      entre <strong>15 minutes et 48 heures</strong>. Votre site sera
                      accessible sur <strong>{deployment.url}</strong> une fois la
                      propagation termin&eacute;e.
                    </p>
                  </div>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 w-7 h-7 rounded-full bg-green-100 text-green-700
                    text-sm font-bold flex items-center justify-center">
                    5
                  </span>
                  <div>
                    <p className="text-sm font-medium text-gray-800">
                      V&eacute;rifiez que tout fonctionne
                    </p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      Testez en ouvrant{" "}
                      <a
                        href={deployment.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-blue-600 hover:underline"
                      >
                        {deployment.url}
                      </a>{" "}
                      dans votre navigateur. Si le certificat SSL est en attente,
                      il sera automatiquement provisionn&eacute; par Google (jusqu&apos;&agrave; 24h).
                    </p>
                  </div>
                </li>
              </ol>
            </div>

            {/* Quick tip */}
            <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg">
              <p className="text-xs text-amber-800">
                <strong>Astuce :</strong> Pour v&eacute;rifier la propagation DNS,
                utilisez{" "}
                <a
                  href={`https://dnschecker.org/#NS/${deployment.domain}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-amber-700 underline hover:text-amber-900"
                >
                  dnschecker.org
                </a>
                {" "}et v&eacute;rifiez que les nameservers Google apparaissent bien.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Subdomain confirmation (no nameserver change needed) */}
      {isSubdomain && (
        <div className="bg-white rounded-xl border-2 border-green-200 p-6">
          <div className="flex items-start gap-3">
            <Server className="w-5 h-5 text-green-600 flex-shrink-0 mt-0.5" />
            <div>
              <h3 className="text-sm font-bold text-green-800">
                DNS configur&eacute; automatiquement
              </h3>
              <p className="mt-1 text-sm text-green-700">
                Le sous-domaine <strong>{deployment.domain}</strong> a &eacute;t&eacute; ajout&eacute;
                &agrave; la zone DNS existante du domaine parent. Aucune configuration
                suppl&eacute;mentaire n&apos;est n&eacute;cessaire.
              </p>
              <p className="mt-2 text-xs text-green-600">
                Le certificat SSL sera provisionn&eacute; automatiquement par Google
                (peut prendre jusqu&apos;&agrave; 24h). Votre site sera accessible sur{" "}
                <a
                  href={deployment.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-medium underline"
                >
                  {deployment.url}
                </a>
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Infrastructure recap card */}
      {isSuccess && deployment.infra_details && (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          <div className="bg-gray-50 px-6 py-3 border-b border-gray-200">
            <h3 className="text-sm font-bold text-gray-800 flex items-center gap-2">
              <Server className="w-4 h-4 text-gray-500" />
              Ressources GCP provisionnees
            </h3>
          </div>
          <div className="p-6">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {/* Domain */}
              {deployment.domain && (
                <div className="flex items-start gap-3">
                  <Globe className="w-4 h-4 text-emerald-500 mt-0.5 flex-shrink-0" />
                  <div>
                    <dt className="text-xs text-gray-500 uppercase tracking-wide">Domaine</dt>
                    <dd className="text-sm font-mono font-medium text-gray-900">{deployment.domain}</dd>
                  </div>
                </div>
              )}

              {/* Storage Bucket */}
              {deployment.storage_bucket && (
                <div className="flex items-start gap-3">
                  <Database className="w-4 h-4 text-blue-500 mt-0.5 flex-shrink-0" />
                  <div>
                    <dt className="text-xs text-gray-500 uppercase tracking-wide">Bucket GCS</dt>
                    <dd className="text-sm font-mono font-medium text-gray-900 break-all">{deployment.storage_bucket}</dd>
                  </div>
                </div>
              )}

              {/* CDN */}
              {deployment.backend_bucket && (
                <div className="flex items-start gap-3">
                  <Zap className="w-4 h-4 text-amber-500 mt-0.5 flex-shrink-0" />
                  <div>
                    <dt className="text-xs text-gray-500 uppercase tracking-wide">CDN (Backend Bucket)</dt>
                    <dd className="text-sm font-mono font-medium text-gray-900 break-all">{deployment.backend_bucket}</dd>
                  </div>
                </div>
              )}

              {/* Load Balancer */}
              {deployment.infra_details?.lb_name && (
                <div className="flex items-start gap-3">
                  <Link2 className="w-4 h-4 text-purple-500 mt-0.5 flex-shrink-0" />
                  <div>
                    <dt className="text-xs text-gray-500 uppercase tracking-wide">Load Balancer</dt>
                    <dd className="text-sm font-mono font-medium text-gray-900">{deployment.infra_details.lb_name}</dd>
                  </div>
                </div>
              )}

              {/* SSL */}
              {deployment.mode === "prod" && deployment.infra_details?.ssl_cert_created && (
                <div className="flex items-start gap-3">
                  <Shield className="w-4 h-4 text-green-500 mt-0.5 flex-shrink-0" />
                  <div>
                    <dt className="text-xs text-gray-500 uppercase tracking-wide">Certificat SSL</dt>
                    <dd className="text-sm font-medium text-gray-900">
                      Google-managed
                      <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-green-100 text-green-700">
                        Auto-provisionne
                      </span>
                    </dd>
                  </div>
                </div>
              )}

              {/* Cloud Run direct URL */}
              {deployment.infra_details?.run_app_url && (
                <div className="flex items-start gap-3">
                  <Link2 className="w-4 h-4 text-teal-500 mt-0.5 flex-shrink-0" />
                  <div>
                    <dt className="text-xs text-gray-500 uppercase tracking-wide">Cloud Run URL</dt>
                    <dd className="text-sm font-mono font-medium text-gray-900 break-all">
                      {deployment.infra_details.run_app_url}
                    </dd>
                  </div>
                </div>
              )}

              {/* DNS Zone */}
              {deployment.mode === "prod" && deployment.infra_details?.dns_zone_created && (
                <div className="flex items-start gap-3">
                  <Globe className="w-4 h-4 text-indigo-500 mt-0.5 flex-shrink-0" />
                  <div>
                    <dt className="text-xs text-gray-500 uppercase tracking-wide">Zone DNS</dt>
                    <dd className="text-sm font-medium text-gray-900">
                      Cloud DNS
                      <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-indigo-100 text-indigo-700">
                        Records A + CNAME
                      </span>
                    </dd>
                  </div>
                </div>
              )}
            </div>

            {deployment.infra_details?.resources?.length > 0 && (
              <div className="mt-5 pt-4 border-t border-gray-100">
                <dt className="text-xs text-gray-500 uppercase tracking-wide mb-2">
                  Resources of this website
                </dt>
                <ul className="flex flex-col gap-1">
                  {deployment.infra_details.resources.map((r) => (
                    <li key={`${r.kind}:${r.name}`} className="text-xs text-gray-700">
                      <span className="inline-block min-w-[9rem] text-gray-500">
                        {RESOURCE_LABELS[r.kind] || r.kind}
                      </span>
                      <span className="font-mono break-all">{r.name}</span>
                      {r.shared_bucket && (
                        <span className="ml-1 text-gray-400">(in the shared demo bucket)</span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Metadata card */}
      <div className="bg-white rounded-xl border border-gray-200 p-6">
        <h3 className="text-sm font-semibold text-gray-700 mb-4">
          Deployment Details
        </h3>
        <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="flex items-start gap-3">
            <Globe className="w-4 h-4 text-gray-400 mt-0.5 flex-shrink-0" />
            <div>
              <dt className="text-xs text-gray-500 uppercase tracking-wide">
                Website Name
              </dt>
              <dd className="text-sm font-medium text-gray-900">
                {deployment.website_name}
              </dd>
            </div>
          </div>

          <div className="flex items-start gap-3">
            <Tag className="w-4 h-4 text-gray-400 mt-0.5 flex-shrink-0" />
            <div>
              <dt className="text-xs text-gray-500 uppercase tracking-wide">
                Mode
              </dt>
              <dd className="text-sm font-medium text-gray-900 capitalize">
                {deployment.mode}
              </dd>
            </div>
          </div>

          {deployment.domain && (
            <div className="flex items-start gap-3">
              <Globe className="w-4 h-4 text-gray-400 mt-0.5 flex-shrink-0" />
              <div>
                <dt className="text-xs text-gray-500 uppercase tracking-wide">
                  Domain
                </dt>
                <dd className="text-sm font-medium text-gray-900">
                  {deployment.domain}
                </dd>
              </div>
            </div>
          )}

          <div className="flex items-start gap-3">
            <Clock className="w-4 h-4 text-gray-400 mt-0.5 flex-shrink-0" />
            <div>
              <dt className="text-xs text-gray-500 uppercase tracking-wide">
                Started
              </dt>
              <dd className="text-sm font-medium text-gray-900">
                {formatDate(deployment.created_at)}
              </dd>
            </div>
          </div>

          <div className="flex items-start gap-3">
            <Clock className="w-4 h-4 text-gray-400 mt-0.5 flex-shrink-0" />
            <div>
              <dt className="text-xs text-gray-500 uppercase tracking-wide">
                Completed
              </dt>
              <dd className="text-sm font-medium text-gray-900">
                {formatDate(deployment.completed_at)}
              </dd>
            </div>
          </div>
        </dl>
      </div>
    </div>
  );
}
