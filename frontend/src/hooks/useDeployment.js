import { useEffect, useRef, useState, useCallback } from "react";
import { getDeployment } from "../services/api";

/**
 * Custom hook that polls a deployment's status.
 * Polls every 2 seconds while the deployment is "queued" or "running".
 * Stops polling once the status becomes "success" or "failed".
 *
 * @param {string|null} deploymentId - The deployment ID to watch.
 * @returns {{ deployment: object|null, loading: boolean, error: string|null, refetch: Function }}
 */
export default function useDeployment(deploymentId) {
  const [deployment, setDeployment] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const intervalRef = useRef(null);
  const mountedRef = useRef(true);

  const fetchDeployment = useCallback(async () => {
    if (!deploymentId) return;

    try {
      const data = await getDeployment(deploymentId);
      if (!mountedRef.current) return;

      setDeployment(data);
      setError(null);

      // Stop polling if we've reached a terminal state
      if (data.status === "success" || data.status === "failed") {
        if (intervalRef.current) {
          clearInterval(intervalRef.current);
          intervalRef.current = null;
        }
      }
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err.response?.data?.detail || err.message || "Failed to fetch deployment");
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  }, [deploymentId]);

  const refetch = useCallback(() => {
    setLoading(true);
    fetchDeployment();
  }, [fetchDeployment]);

  useEffect(() => {
    mountedRef.current = true;
    setLoading(true);
    setError(null);
    setDeployment(null);

    if (!deploymentId) {
      setLoading(false);
      return;
    }

    // Initial fetch
    fetchDeployment();

    // Start polling every 2 seconds
    intervalRef.current = setInterval(fetchDeployment, 2000);

    return () => {
      mountedRef.current = false;
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [deploymentId, fetchDeployment]);

  return { deployment, loading, error, refetch };
}
