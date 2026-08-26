#!/usr/bin/env bash
# One-time project setup for deploying MotoRooter to Cloud Run.
#
# Idempotent: every step checks before it creates, so re-running it after a partial failure is
# safe and does nothing where the resource already exists.
#
# Reads the five API keys out of backend/.env and pipes them into Secret Manager without ever
# printing them. It does not commit them anywhere and does not echo them, so this can be run
# with someone watching.
#
#   infra/bootstrap.sh          # do it
#   infra/bootstrap.sh --dry    # print what it would do and change nothing
set -euo pipefail

DRY=""
[ "${1:-}" = "--dry" ] && DRY="echo [dry] "

cd "$(git rev-parse --show-toplevel)"

PROJECT=$(gcloud config get-value project 2>/dev/null)
[ -n "$PROJECT" ] || { echo "no gcloud project set: gcloud config set project <id>"; exit 1; }
REGION=us-west1
SERVICE=motorooter
BUCKET="${PROJECT}-motorooter-trips"
NUMBER=$(gcloud projects describe "$PROJECT" --format="value(projectNumber)")

echo "project $PROJECT ($NUMBER)  region $REGION"
echo

# ── APIs ────────────────────────────────────────────────────────────────────────────────────
# The Maps APIs are already on — these are the four the deploy itself needs.
for api in cloudbuild run artifactregistry secretmanager storage-api; do
  if gcloud services list --enabled --format="value(config.name)" | grep -q "^${api}.googleapis.com$"; then
    echo "api ${api}: already enabled"
  else
    echo "api ${api}: enabling"
    $DRY gcloud services enable "${api}.googleapis.com"
  fi
done
echo

# ── Artifact Registry ───────────────────────────────────────────────────────────────────────
# cloudbuild.yaml pushes to ${REGION}-docker.pkg.dev/$PROJECT/motorooter/motorooter, and the
# repository has to exist first — a missing one fails the push rather than creating it.
if gcloud artifacts repositories describe "$SERVICE" --location="$REGION" >/dev/null 2>&1; then
  echo "artifact registry: already exists"
else
  echo "artifact registry: creating"
  $DRY gcloud artifacts repositories create "$SERVICE" \
    --repository-format=docker --location="$REGION" \
    --description="MotoRooter service images"
fi
echo

# ── Bucket ──────────────────────────────────────────────────────────────────────────────────
# Uniform bucket-level access, and no public grant: trips are reachable through the app's API,
# which is a different thing from the bucket being browsable.
if gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  echo "bucket: already exists"
else
  echo "bucket: creating gs://${BUCKET}"
  $DRY gcloud storage buckets create "gs://${BUCKET}" \
    --location="$REGION" --uniform-bucket-level-access
fi
echo

# ── Secrets ─────────────────────────────────────────────────────────────────────────────────
# Names must match cloudbuild.yaml exactly. Values come from backend/.env and are piped, never
# echoed and never passed as an argument (arguments are visible in the process list).
secret_from_env() {
  local name=$1 var=$2
  local value
  value=$(grep -oP "(?<=^${var}=).*" backend/.env || true)
  if [ -z "$value" ]; then
    echo "secret ${name}: SKIPPED — ${var} not found in backend/.env"
    return
  fi
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    echo "secret ${name}: exists, adding a new version"
    [ -n "$DRY" ] && { echo "[dry] would add version to ${name}"; return; }
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
  else
    echo "secret ${name}: creating"
    [ -n "$DRY" ] && { echo "[dry] would create ${name}"; return; }
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic >/dev/null
  fi
}

secret_from_env ors-api-key              ORS_API_KEY
secret_from_env google-maps-server-key   GOOGLE_MAPS_SERVER_KEY
secret_from_env google-maps-browser-key  GOOGLE_MAPS_BROWSER_KEY
secret_from_env openai-api-key           OPENAI_API_KEY
secret_from_env brave-search-api-key     BRAVE_SEARCH_API_KEY
echo

# ── IAM ─────────────────────────────────────────────────────────────────────────────────────
# Three grants, and each one fails differently if you skip it:
#   runtime + secretAccessor  -> the service crashes on boot, secrets unreadable
#   runtime + objectAdmin     -> healthy service, every trip save 403s
#   build   + secretAccessor  -> the build fails reading the browser key
RUNTIME="${NUMBER}-compute@developer.gserviceaccount.com"
BUILD="${NUMBER}@cloudbuild.gserviceaccount.com"

for s in ors-api-key google-maps-server-key google-maps-browser-key openai-api-key brave-search-api-key; do
  $DRY gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUNTIME}" --role=roles/secretmanager.secretAccessor >/dev/null 2>&1 || true
done
echo "iam: runtime service account can read the five secrets"

$DRY gcloud secrets add-iam-policy-binding google-maps-browser-key \
  --member="serviceAccount:${BUILD}" --role=roles/secretmanager.secretAccessor >/dev/null 2>&1 || true
echo "iam: build service account can read the browser key"

$DRY gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUNTIME}" --role=roles/storage.objectAdmin >/dev/null 2>&1 || true
echo "iam: runtime service account can read and write the bucket"

# Cloud Build deploys the service, which needs both of these.
for role in roles/run.admin roles/iam.serviceAccountUser; do
  $DRY gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${BUILD}" --role="$role" >/dev/null 2>&1 || true
done
echo "iam: build service account can deploy to Cloud Run"
echo

echo "Setup complete. Next:"
echo "    make deploy"
echo
echo "Then add the service URL to the browser key's HTTP referrer list, or the map will be"
echo "refused on the deployed origin — which looks like a broken key rather than a missing"
echo "referrer. The deploy prints the URL; it should be:"
echo "    https://${SERVICE}-${NUMBER}.${REGION}.run.app/*"
