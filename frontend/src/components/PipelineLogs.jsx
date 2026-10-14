import { useEffect, useRef } from "react";
import {
  CheckCircle,
  XCircle,
  Loader,
  Circle,
  MinusCircle,
  Clock,
} from "lucide-react";
import useWebSocket from "../hooks/useWebSocket";

const PIPELINE_STEPS = [
  { key: "EXTRACT", label: "Extract" },
  { key: "AI_INSPECT", label: "AI Inspect" },
  { key: "AI_FIX", label: "AI Fix" },
  { key: "BUILD", label: "Build" },
  { key: "VERIFY", label: "Verify" },
  { key: "DOMAIN_REGISTER", label: "Domain" },
  { key: "INFRA", label: "Infrastructure" },
  { key: "UPLOAD", label: "Upload" },
  { key: "NOTIFY", label: "Notify" },
];

const STATUS_CONFIG = {
  pending: {
    icon: Circle,
    color: "text-gray-400",
    bg: "bg-gray-100",
    ring: "ring-gray-200",
  },
  running: {
    icon: Loader,
    color: "text-[#2563EB]",
    bg: "bg-blue-50",
    ring: "ring-blue-200",
    animate: true,
  },
  completed: {
    icon: CheckCircle,
    color: "text-[#16a34a]",
    bg: "bg-green-50",
    ring: "ring-green-200",
  },
  failed: {
    icon: XCircle,
    color: "text-[#dc2626]",
    bg: "bg-red-50",
    ring: "ring-red-200",
  },
  skipped: {
    icon: MinusCircle,
    color: "text-gray-400",
    bg: "bg-gray-50",
    ring: "ring-gray-200",
  },
};

/**
 * Formats a duration in seconds to a human-readable string.
 */
function formatDuration(seconds) {
  if (seconds == null) return null;
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}

/**
 * Detects the log level from a log line for coloring.
 */
function getLogLevel(line) {
  const lower = line.toLowerCase();
  if (lower.includes("error") || lower.includes("fail") || lower.includes("exception")) {
    return "error";
  }
  if (lower.includes("warn")) {
    return "warn";
  }
  return "info";
}

/**
 * Real-time pipeline viewer with step stepper and terminal log output.
 *
 * @param {{ deploymentId: string, steps: Array, status: string }} props
 *   steps: array of { name, status, started_at?, completed_at?, duration_seconds? }
 */
export default function PipelineLogs({ deploymentId, steps = [], status }) {
  const { logs, connected } = useWebSocket(deploymentId);
  const logsEndRef = useRef(null);
  const logContainerRef = useRef(null);

  // Build a map from step key to its data
  const stepMap = {};
  for (const s of steps) {
    stepMap[s.name] = s;
  }

  // Auto-scroll to bottom when new logs arrive
  useEffect(() => {
    if (logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [logs]);

  return (
    <div className="space-y-6">
      {/* Pipeline stepper */}
      <div className="bg-white rounded-xl border border-gray-200 p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">Pipeline</h2>
          <div className="flex items-center gap-2 text-sm">
            <span
              className={`inline-block w-2 h-2 rounded-full ${
                connected ? "bg-green-500" : "bg-gray-400"
              }`}
            />
            <span className="text-gray-500">
              {connected ? "Live" : "Connecting..."}
            </span>
          </div>
        </div>

        {/* Horizontal step list */}
        <div className="flex items-start gap-1 overflow-x-auto pb-2">
          {PIPELINE_STEPS.map((step, idx) => {
            const data = stepMap[step.key] || { status: "pending" };
            const config = STATUS_CONFIG[data.status] || STATUS_CONFIG.pending;
            const Icon = config.icon;
            const isLast = idx === PIPELINE_STEPS.length - 1;

            return (
              <div key={step.key} className="flex items-start flex-1 min-w-0">
                <div className="flex flex-col items-center text-center flex-1">
                  {/* Step icon */}
                  <div
                    className={`flex items-center justify-center w-10 h-10 rounded-full
                      ${config.bg} ring-2 ${config.ring} transition-all`}
                  >
                    <Icon
                      className={`w-5 h-5 ${config.color} ${
                        config.animate ? "animate-spin" : ""
                      }`}
                    />
                  </div>

                  {/* Step label */}
                  <p
                    className={`mt-2 text-xs font-medium truncate max-w-full ${
                      data.status === "running"
                        ? "text-[#2563EB] font-semibold"
                        : "text-gray-600"
                    }`}
                  >
                    {step.label}
                  </p>

                  {/* Duration */}
                  {data.duration_seconds != null && (
                    <div className="flex items-center gap-0.5 mt-0.5">
                      <Clock className="w-3 h-3 text-gray-400" />
                      <span className="text-[10px] text-gray-400">
                        {formatDuration(data.duration_seconds)}
                      </span>
                    </div>
                  )}
                </div>

                {/* Connector line */}
                {!isLast && (
                  <div className="flex items-center pt-5 px-0.5">
                    <div
                      className={`h-0.5 w-4 sm:w-6 ${
                        data.status === "completed"
                          ? "bg-green-300"
                          : "bg-gray-200"
                      }`}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Terminal log viewer */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 bg-gray-900 border-b border-gray-800">
          <div className="flex items-center gap-2">
            {/* Fake terminal dots */}
            <span className="w-3 h-3 rounded-full bg-red-500" />
            <span className="w-3 h-3 rounded-full bg-yellow-500" />
            <span className="w-3 h-3 rounded-full bg-green-500" />
            <span className="ml-3 text-sm text-gray-400 font-mono">
              deployment logs
            </span>
          </div>
          <span className="text-xs text-gray-500 font-mono">
            {logs.length} lines
          </span>
        </div>

        <div
          ref={logContainerRef}
          className="terminal terminal-scrollbar h-96 overflow-y-auto"
        >
          {logs.length === 0 && (
            <div className="flex items-center justify-center h-full">
              <p className="text-gray-500 font-mono text-sm">
                {status === "queued"
                  ? "Waiting for deployment to start..."
                  : "Waiting for logs..."}
              </p>
            </div>
          )}

          {logs.map((line, idx) => {
            const level = getLogLevel(line);
            return (
              <div key={idx} className="terminal-line">
                <span
                  className={
                    level === "error"
                      ? "terminal-line-error"
                      : level === "warn"
                      ? "terminal-line-warn"
                      : "terminal-line-info"
                  }
                >
                  {line}
                </span>
              </div>
            );
          })}

          <div ref={logsEndRef} />
        </div>
      </div>
    </div>
  );
}
