#!/usr/bin/env bash
set -euo pipefail

# ─── Configuration ──────────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="europe-west1"
REPO="europe-west1-docker.pkg.dev/${PROJECT_ID}/cloud-run-images"

BACKEND_IMAGE="${REPO}/webdeploy-backend:staging"
FRONTEND_IMAGE="${REPO}/webdeploy-frontend:staging"

BACKEND_SERVICE="webdeploy-backend-staging"
FRONTEND_SERVICE="webdeploy-frontend-staging"

# Service account used by the Cloud Run services at runtime
SERVICE_ACCOUNT="webdeploy-runtime@${PROJECT_ID}.iam.gserviceaccount.com"

# ─── Step 1: Build & push backend image ─────────────────────────────
echo "══════════════════════════════════════════════════════════════"
echo "  1/4  Building backend image..."
echo "══════════════════════════════════════════════════════════════"
gcloud builds submit \
  --project="${PROJECT_ID}" \
  --config=cloudbuild-backend-staging.yaml \
  --timeout=600s

# ─── Step 2: Deploy backend to Cloud Run ─────────────────────────────
echo "══════════════════════════════════════════════════════════════"
echo "  2/4  Deploying backend service: ${BACKEND_SERVICE}..."
echo "══════════════════════════════════════════════════════════════"
gcloud run deploy "${BACKEND_SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${BACKEND_IMAGE}" \
  --platform=managed \
  --allow-unauthenticated \
  --service-account="${SERVICE_ACCOUNT}" \
  --memory=1Gi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=5 \
  --timeout=300s \
  --set-env-vars="PROJECT_ID=${PROJECT_ID}" \
  --set-env-vars="GOOGLE_APPLICATION_CREDENTIALS=" \
  --set-env-vars="DEMO_DOMAIN=example.com" \
  --set-env-vars="DEMO_URL_MAP_NAME=legacy-demo-lb" \
  --set-env-vars="DEMO_GLOBAL_IP_NAME=legacy-demo-lb-ip" \
  --set-env-vars="PROD_URL_MAP_NAME=websites-urlmap-prod" \
  --set-env-vars="PROD_GLOBAL_IP_NAME=websites-lb-ip-prod" \
  --set-env-vars="PROD_HTTPS_PROXY_NAME=websites-https-proxy-prod" \
  --set-env-vars="PROD_AUTO_REGISTER_DOMAINS=false" \
  --set-env-vars="PROD_AUTO_CREATE_DNS_ZONE=false" \
  --set-env-vars="PROD_AUTO_CREATE_SSL_CERT=false" \
  --set-env-vars="AI_PROVIDERS=gemini,openrouter" \
  --set-env-vars="GEMINI_API_KEY=${GEMINI_API_KEY:-}" \
  --set-env-vars="OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}" \
  --set-env-vars="OPENROUTER_MODEL=meta-llama/llama-3.1-8b-instruct:free" \
  --set-env-vars="GMAIL_DELEGATED_USER=admin@example.com" \
  --set-env-vars="NOTIFICATION_FROM_EMAIL=admin@example.com" \
  --set-env-vars="NOTIFICATION_TO_EMAILS=team@example.com" \
  --set-env-vars="ADMIN_APPROVAL_EMAIL=admin@example.com" \
  --set-env-vars="APPROVAL_TOKEN_SECRET=${APPROVAL_TOKEN_SECRET:?set APPROVAL_TOKEN_SECRET}" \
  --set-env-vars="GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:?set GOOGLE_CLIENT_ID}" \
  --set-env-vars="UPLOAD_DIR=./uploads" \
  --set-env-vars="TEMP_DIR=./tmp" \
  --set-env-vars="LOG_LEVEL=DEBUG" \
  --set-env-vars="MAX_ZIP_SIZE_MB=500" \
  --set-env-vars="BUILD_TIMEOUT_SECONDS=300" \
  --set-env-vars="PREVIEW_TIMEOUT_SECONDS=30"

# ─── Step 3: Get backend URL & build frontend ───────────────────────
BACKEND_URL=$(gcloud run services describe "${BACKEND_SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)")

echo "══════════════════════════════════════════════════════════════"
echo "  3/4  Building frontend image..."
echo "  Backend URL: ${BACKEND_URL}"
echo "══════════════════════════════════════════════════════════════"

gcloud builds submit \
  --project="${PROJECT_ID}" \
  --config=cloudbuild-frontend-staging.yaml \
  --timeout=600s

# ─── Step 4: Deploy frontend to Cloud Run ────────────────────────────
# Extract the host from the backend URL (without https://)
BACKEND_HOST=$(echo "${BACKEND_URL}" | sed 's|https://||')

echo "══════════════════════════════════════════════════════════════"
echo "  4/4  Deploying frontend service: ${FRONTEND_SERVICE}..."
echo "  BACKEND_URL: ${BACKEND_URL}"
echo "  BACKEND_HOST: ${BACKEND_HOST}"
echo "══════════════════════════════════════════════════════════════"

gcloud run deploy "${FRONTEND_SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${FRONTEND_IMAGE}" \
  --platform=managed \
  --allow-unauthenticated \
  --memory=256Mi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=3 \
  --timeout=60s \
  --set-env-vars="PORT=8080" \
  --set-env-vars="BACKEND_URL=${BACKEND_URL}" \
  --set-env-vars="BACKEND_HOST=${BACKEND_HOST}"

# ─── Done ────────────────────────────────────────────────────────────
FRONTEND_URL=$(gcloud run services describe "${FRONTEND_SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)")

# Update backend with the correct FRONTEND_URL for CORS
echo "══════════════════════════════════════════════════════════════"
echo "  Updating backend FRONTEND_URL for CORS..."
echo "══════════════════════════════════════════════════════════════"
gcloud run services update "${BACKEND_SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --update-env-vars="FRONTEND_URL=${FRONTEND_URL}"

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  STAGING DEPLOYMENT COMPLETE"
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "  Frontend (staging): ${FRONTEND_URL}"
echo "  Backend  (staging): ${BACKEND_URL}"
echo ""
echo "  These are separate from your production services."
echo "══════════════════════════════════════════════════════════════"
