import { useEffect, useRef, useState } from "react";

/**
 * Custom hook for real-time log streaming via WebSocket.
 * Connects to ws://<host>/ws/logs/<deploymentId>.
 * Auto-reconnects on disconnect with exponential backoff.
 *
 * Handles React 18 StrictMode correctly by using a per-effect
 * "cancelled" flag so that stale connections from the first
 * mount/unmount cycle never add messages or trigger reconnects.
 *
 * @param {string|null} deploymentId - The deployment ID to subscribe to.
 * @returns {{ logs: string[], connected: boolean }}
 */
export default function useWebSocket(deploymentId) {
  const [logs, setLogs] = useState([]);
  const [connected, setConnected] = useState(false);
  const maxReconnectAttempts = 20;
  // Keep a ref so reconnect closures can check freshness
  const activeIdRef = useRef(0);

  useEffect(() => {
    if (!deploymentId) return;

    // Each effect invocation gets a unique ID.
    // When cleanup runs (StrictMode or real unmount), we increment
    // the ref so any stale closures from the old WS become no-ops.
    const connectionId = ++activeIdRef.current;
    const isCurrent = () => activeIdRef.current === connectionId;

    let ws = null;
    let reconnectTimeout = null;
    let reconnectAttempts = 0;

    setLogs([]);

    function connect() {
      if (!isCurrent()) return;

      // Build the WebSocket URL
      let wsUrl;
      const apiUrl = import.meta.env.VITE_API_URL;
      if (apiUrl) {
        const base = apiUrl.replace(/^http/, "ws");
        wsUrl = `${base}/ws/logs/${deploymentId}`;
      } else {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        wsUrl = `${protocol}//${window.location.host}/ws/logs/${deploymentId}`;
      }

      try {
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
          if (!isCurrent()) { ws.close(); return; }
          setConnected(true);
          reconnectAttempts = 0;
        };

        ws.onmessage = (event) => {
          if (!isCurrent()) return;
          // Ignore empty keepalive pings from the server
          if (event.data && event.data.trim()) {
            setLogs((prev) => [...prev, event.data]);
          }
        };

        ws.onclose = () => {
          if (!isCurrent()) return;
          setConnected(false);
          ws = null;

          // Auto-reconnect with exponential backoff
          if (reconnectAttempts < maxReconnectAttempts && isCurrent()) {
            const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000);
            reconnectAttempts += 1;
            reconnectTimeout = setTimeout(connect, delay);
          }
        };

        ws.onerror = () => {
          // onclose fires after onerror — reconnect logic handled there
          ws.close();
        };
      } catch (err) {
        console.error("WebSocket connection error:", err);
      }
    }

    connect();

    return () => {
      // Invalidate this connection's closures
      activeIdRef.current += 1;

      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
      }
      if (ws) {
        ws.close();
        ws = null;
      }
      setConnected(false);
    };
  }, [deploymentId]);

  return { logs, connected };
}
