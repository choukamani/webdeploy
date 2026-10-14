import { useParams, Link } from "react-router-dom";
import { ArrowLeft, Loader } from "lucide-react";
import useDeployment from "../hooks/useDeployment";
import PipelineLogs from "../components/PipelineLogs";
import DeploymentStatus from "../components/DeploymentStatus";

/**
 * Transform the API deployment response into the shape the UI components expect.
 * API returns: { steps_status: {EXTRACT: "completed", ...}, result_url, ai_summary }
 * Components expect: { steps: [{name, status}], url, ai_summary }
 */
function normalizeDeployment(d) {
  if (!d) return null;
  // Transform steps_status dict → steps array
  const stepsDict = d.steps_status || {};
  const steps = Object.entries(stepsDict).map(([name, status]) => ({
    name,
    status,
  }));
  return {
    ...d,
    steps,
    url: d.result_url || d.url,
    ai_summary: d.ai_summary,
    dns_nameservers: d.dns_nameservers || [],
    storage_bucket: d.storage_bucket || null,
    backend_bucket: d.backend_bucket || null,
    infra_details: d.infra_details || null,
  };
}

/**
 * Deployment detail page.
 * Shows pipeline stepper + real-time logs while running,
 * and deployment result banner when completed.
 */
export default function DeploymentDetail() {
  const { id } = useParams();
  const { deployment: rawDeployment, loading, error, refetch } = useDeployment(id);
  const deployment = normalizeDeployment(rawDeployment);

  if (loading && !deployment) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader className="w-8 h-8 text-[#2563EB] animate-spin" />
      </div>
    );
  }

  if (error && !deployment) {
    return (
      <div className="max-w-2xl mx-auto text-center py-16">
        <p className="text-red-600 font-medium mb-4">{error}</p>
        <button
          onClick={refetch}
          className="inline-flex items-center gap-2 px-4 py-2 bg-[#2563EB] text-white
            rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
        >
          Retry
        </button>
      </div>
    );
  }

  const isTerminal =
    deployment?.status === "success" || deployment?.status === "failed";

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <Link
          to="/history"
          className="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-[#2563EB]
            transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          Back to History
        </Link>

        {deployment && (
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold text-gray-900">
              {deployment.website_name}
            </h1>
            <span
              className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs
                font-medium ring-1 ring-inset capitalize
                ${
                  deployment.status === "queued"
                    ? "bg-gray-100 text-gray-700 ring-gray-300"
                    : deployment.status === "running"
                    ? "bg-blue-100 text-blue-700 ring-blue-300"
                    : deployment.status === "success"
                    ? "bg-green-100 text-green-700 ring-green-300"
                    : "bg-red-100 text-red-700 ring-red-300"
                }`}
            >
              {deployment.status}
            </span>
          </div>
        )}
      </div>

      {/* Result banner (only when terminal) */}
      {isTerminal && <DeploymentStatus deployment={deployment} />}

      {/* Pipeline stepper + logs (always shown) */}
      {deployment && (
        <PipelineLogs
          deploymentId={id}
          steps={deployment.steps || []}
          status={deployment.status}
        />
      )}
    </div>
  );
}
