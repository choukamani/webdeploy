import axios from "axios";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || "",
  headers: {
    Accept: "application/json",
  },
});

/**
 * Deploy a website by uploading a zip file with configuration.
 * @param {FormData} formData - Must include: zip_file, mode, website_name.
 *   Optional: domain, notification_emails.
 * @returns {Promise<object>} The created deployment object.
 */
export async function deployWebsite(formData) {
  const response = await api.post("/api/deploy", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 600000, // 10 minutes — folder uploads can be large
    maxContentLength: Infinity,
    maxBodyLength: Infinity,
  });
  return response.data;
}

/**
 * Deploy from a Git commit (no upload needed).
 * @param {object} body - { connection_id, commit_sha, push_event_id?, mode,
 *   website_name, domain?, notification_emails?, deployer_*, ai_enabled,
 *   domain_purchase_confirmed }
 */
export async function deployFromGit(body) {
  const response = await api.post("/api/deploy/from-git", body, {
    timeout: 180000,
  });
  return response.data;
}

/**
 * List deployments, newest first, optionally restricted to a date range.
 * @param {object} [options]
 * @param {string} [options.startDate] - Earliest day to include, YYYY-MM-DD.
 * @param {string} [options.endDate] - Latest day to include, YYYY-MM-DD.
 * @param {number} [options.limit] - Page size (server default 1000, max 5000).
 * @param {number} [options.offset] - Rows to skip.
 * @returns {Promise<{deployments: object[], total: number}>} The page, plus how
 *   many deployments match the range in total (may exceed the page length).
 */
export async function getDeployments({
  startDate, endDate, modes, limit, offset,
} = {}) {
  // URLSearchParams so repeated `mode` params stay `mode=a&mode=b` — axios
  // would otherwise serialise the array as `mode[]=`, which FastAPI ignores.
  const params = new URLSearchParams();
  if (startDate) params.append("start_date", startDate);
  if (endDate) params.append("end_date", endDate);
  if (limit) params.append("limit", limit);
  if (offset) params.append("offset", offset);
  (modes || []).forEach((mode) => params.append("mode", mode));

  const response = await api.get("/api/deployments", { params });
  const deployments = Array.isArray(response.data) ? response.data : [];
  const total = Number(response.headers["x-total-count"]);

  return {
    deployments,
    total: Number.isFinite(total) ? total : deployments.length,
  };
}

/**
 * Get a single deployment by ID.
 * @param {string} id - Deployment ID.
 * @returns {Promise<object>} Deployment object with steps and metadata.
 */
export async function getDeployment(id) {
  const response = await api.get(`/api/deployments/${id}`);
  return response.data;
}

/**
 * Get logs for a deployment.
 * @param {string} id - Deployment ID.
 * @returns {Promise<object>} Logs data.
 */
export async function getDeploymentLogs(id) {
  const response = await api.get(`/api/deployments/${id}/logs`);
  return response.data;
}

/**
 * Remove a single deployment record from history, leaving the website (and
 * its GCP resources) untouched. Refused by the server for the live or only
 * version — delete the website instead.
 * @param {string} id - Deployment ID.
 */
export async function deleteHistoryEntry(id) {
  const response = await api.delete(`/api/deployments/${id}`, {
    params: { scope: "record" },
  });
  return response.data;
}

/** Websites per delete request — mirrors the server's MAX_WEBSITES_PER_REQUEST. */
export const WEBSITE_DELETE_CHUNK = 10;

/**
 * Delete websites (every GCP resource and every deployment record of each).
 * At most WEBSITE_DELETE_CHUNK keys per call.
 * @param {string[]} keys - Website keys, e.g. ["demo:my-site", "prod:shop.com"].
 * @returns {Promise<{results: object[], deleted_count: number,
 *   failed_count: number, blocked_count: number}>} One result per website:
 *   status "deleted", "delete_failed" (retry to resume) or "blocked".
 */
export async function deleteWebsites(keys) {
  const response = await api.post(
    "/api/websites/bulk-delete",
    { keys },
    { timeout: 900000 } // legacy-LB teardowns wait on GCP operations
  );
  return response.data;
}

/**
 * What deploying (mode, name, domain) will do: update an existing website,
 * or create a new one — and at which URL.
 */
export async function lookupWebsite({ mode, websiteName, domain }) {
  const params = { mode, website_name: websiteName };
  if (domain) params.domain = domain;
  const response = await api.get("/api/websites/lookup", { params });
  return response.data;
}

/**
 * Resolve a selection of deployments (explicit ids, or date range + mode) to
 * the websites deleting it would remove. Use `dryRun: true` (the default) for
 * the preview, then delete the previewed website keys with deleteWebsites().
 *
 * @param {object} options
 * @param {string[]} [options.ids] - Explicit selection; ignores the filters.
 * @param {string} [options.startDate] - YYYY-MM-DD.
 * @param {string} [options.endDate] - YYYY-MM-DD.
 * @param {string[]} [options.modes] - e.g. ["demo", "cloudrun"].
 * @param {boolean} [options.includeProd] - Prod is skipped unless set.
 * @param {boolean} [options.dryRun] - Defaults to true server-side.
 * @param {number} [options.expectedCount] - Required when dryRun is false.
 * @returns {Promise<object>} { dry_run, matched, deleted_count, failed_count,
 *   items, warnings }
 */
export async function bulkDeleteDeployments({
  ids, startDate, endDate, modes, includeProd, dryRun, expectedCount,
} = {}) {
  const body = { dry_run: dryRun !== false };
  if (ids) body.ids = ids;
  if (startDate) body.start_date = startDate;
  if (endDate) body.end_date = endDate;
  if (modes?.length) body.modes = modes;
  if (includeProd) body.include_prod = true;
  if (expectedCount != null) body.expected_count = expectedCount;

  const response = await api.post("/api/deployments/bulk-delete", body, {
    timeout: 600000, // tearing down GCP resources one by one is slow
  });
  return response.data;
}

/**
 * Fetch pipeline statistics for a date range.
 * @param {object} params - Query params: preset, start_date, end_date.
 * @returns {Promise<object>} Statistics response.
 */
export async function getStatistics(params = {}) {
  const response = await api.get("/api/statistics", { params });
  return response.data;
}

/**
 * Send on-demand report emails for a date range.
 * @param {object} body - { start_date, end_date, send_to_deployers, send_to_admins }
 * @returns {Promise<object>} { emails_sent, recipients }
 */
export async function sendOnDemandReport(body) {
  const response = await api.post("/api/statistics/send-report", body);
  return response.data;
}

/**
 * Check domain availability/ownership via Cloud Domains API.
 * @param {string} domain - Domain name to check (e.g. "example.com").
 * @returns {Promise<object>} { status, price_amount, price_currency, message }
 */
export async function checkDomain(domain) {
  const response = await api.get("/api/domains/check", { params: { domain } });
  return response.data;
}

// ── Auth API ──────────────────────────────────────────────────────────

/**
 * Exchange a Google ID token (from GIS) for a Firebase Custom Token.
 * @param {string} credential - Google ID token from Google Identity Services.
 * @returns {Promise<{custom_token: string}>}
 */
export async function googleSignIn(credential) {
  const response = await api.post("/api/auth/google-signin", { credential });
  return response.data;
}

/**
 * Sign up (create pending user account).
 * @param {string} requestedRole - "simple_user" or "super_user"
 * @returns {Promise<object>} User profile.
 */
export async function signup(requestedRole) {
  const response = await api.post("/api/auth/signup", {
    requested_role: requestedRole,
  });
  return response.data;
}

/**
 * Get the current user's profile.
 * @returns {Promise<object>} User profile.
 */
export async function getMe() {
  const response = await api.get("/api/auth/me");
  return response.data;
}

/**
 * List all users (admin only).
 * @returns {Promise<object[]>} Array of user objects.
 */
export async function listUsers() {
  const response = await api.get("/api/auth/users");
  return response.data;
}

/**
 * Approve a user (admin or email token). Optionally override the requested role.
 * @param {string} uid - Firebase UID.
 * @param {{role?: string, token?: string}} [options]
 * @returns {Promise<object>} Updated user profile.
 */
export async function approveUser(uid, options = {}) {
  const { role, token } = options;
  const params = token ? { token } : {};
  const body = role ? { role } : null;
  const response = await api.post(`/api/auth/approve/${uid}`, body, { params });
  return response.data;
}

/**
 * Reject a user (admin or email token).
 * @param {string} uid - Firebase UID.
 * @param {string} [token] - Optional rejection token from email link.
 * @returns {Promise<object>} Updated user profile.
 */
export async function rejectUser(uid, token) {
  const params = token ? { token } : {};
  const response = await api.post(`/api/auth/reject/${uid}`, null, { params });
  return response.data;
}

/**
 * Change the role of an approved user (admin only).
 * @param {string} uid
 * @param {string} role - "admin" | "super_user" | "simple_user"
 */
export async function updateUserRole(uid, role) {
  const response = await api.patch(`/api/auth/users/${uid}/role`, { role });
  return response.data;
}

/**
 * Admin creates a user directly (approved, with role).
 * @param {{email: string, display_name?: string, role: string}} payload
 */
export async function adminCreateUser(payload) {
  const response = await api.post("/api/auth/users", payload);
  return response.data;
}

/**
 * Admin deletes a user (Firestore + Firebase Auth).
 */
export async function adminDeleteUser(uid) {
  const response = await api.delete(`/api/auth/users/${uid}`);
  return response.data;
}

// ── Notification Preferences API ──────────────────────────────────────

/**
 * Get the current user's notification preferences.
 * @returns {Promise<object>} Notification preferences.
 */
export async function getNotificationPreferences() {
  const response = await api.get("/api/auth/me/notification-preferences");
  return response.data;
}

/**
 * Update the current user's notification preferences.
 * @param {object} prefs - Partial preferences to update.
 * @returns {Promise<object>} Updated notification preferences.
 */
export async function updateNotificationPreferences(prefs) {
  const response = await api.put("/api/auth/me/notification-preferences", prefs);
  return response.data;
}

// ── Preview API ──────────────────────────────────────────────────────

export async function createPreview(formData) {
  const response = await api.post("/api/preview", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 120000,
    maxContentLength: Infinity,
    maxBodyLength: Infinity,
  });
  return response.data;
}

export async function deletePreview(previewId) {
  const response = await api.delete(`/api/preview/${previewId}`);
  return response.data;
}

// ── Git Integration API ──────────────────────────────────────────────

export async function getGitConnections() {
  const response = await api.get("/api/git/connections");
  return response.data;
}

export async function createGitConnection(data) {
  const response = await api.post("/api/git/connections", data);
  return response.data;
}

export async function deleteGitConnection(id) {
  const response = await api.delete(`/api/git/connections/${id}`);
  return response.data;
}

export async function updateGitBranch(id, branch) {
  const response = await api.patch(`/api/git/connections/${id}/branch`, { branch });
  return response.data;
}

export async function getGitPushEvents(connectionId) {
  const params = connectionId ? { connection_id: connectionId } : {};
  const response = await api.get("/api/git/push-events", { params });
  return response.data;
}

// ── Admin Dashboard API ──────────────────────────────────────────────

export async function getDashboard() {
  const response = await api.get("/api/admin/dashboard");
  return response.data;
}

// ── Quota API ────────────────────────────────────────────────────────

export async function getQuotaDefaults() {
  const response = await api.get("/api/quotas/defaults");
  return response.data;
}

export async function setRoleQuota(role, config) {
  const response = await api.put(`/api/quotas/role/${role}`, config);
  return response.data;
}

export async function getUserQuota(uid) {
  const response = await api.get(`/api/quotas/user/${uid}`);
  return response.data;
}

export async function setUserQuota(uid, config) {
  const response = await api.put(`/api/quotas/user/${uid}`, config);
  return response.data;
}

export async function getMyQuotaUsage() {
  const response = await api.get("/api/quotas/my-usage");
  return response.data;
}

export async function requestQuotaIncrease({ requested_quota, reason }) {
  const response = await api.post("/api/quotas/request-increase", {
    requested_quota,
    reason: reason || "",
  });
  return response.data;
}

export async function listQuotaRequests(params = {}) {
  const response = await api.get("/api/quotas/requests", { params });
  return response.data;
}

export async function getPendingQuotaRequestCount() {
  const response = await api.get("/api/quotas/requests/pending-count");
  return response.data;
}

export async function approveQuotaRequest(id, body = {}) {
  const response = await api.post(`/api/quotas/requests/${id}/approve`, body);
  return response.data;
}

export async function rejectQuotaRequest(id, body = {}) {
  const response = await api.post(`/api/quotas/requests/${id}/reject`, body);
  return response.data;
}

// ── Alerts API ───────────────────────────────────────────────────────

export async function getAlerts(params = {}) {
  const response = await api.get("/api/alerts", { params });
  return response.data;
}

export async function resolveAlert(id) {
  const response = await api.post(`/api/alerts/${id}/resolve`);
  return response.data;
}

export async function getUnresolvedAlertCount() {
  const response = await api.get("/api/alerts/unresolved-count");
  return response.data;
}

export default api;
