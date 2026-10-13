#!/usr/bin/env bash
# Deploy the delivery system to Cloud Run (README section 8).
#   ./deploy.sh deploy   build the image once and deploy the three services
#   ./deploy.sh warm     min-instances = 1 (before the demo)
#   ./deploy.sh cool     min-instances = 0 (normal)
#   ./deploy.sh budget   create a 1 USD budget alert (needs BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX)
#   ./deploy.sh urls     print the three service URLs
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-asia-east1}"
REPO="${REPO:-delivery}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/app:latest"
SERVICES=(dispatch-fixed dispatch-auto web)
# Identity the three services run as. It gets no roles: the services only serve HTTP
# and read the metadata server, which needs no IAM permission.
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-delivery-runtime}"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# Calibrated parameters (README section 8, plan P8). deploy writes the complete env with
# --set-env-vars, so any env var changed by hand in the console is overwritten on the next
# deploy: change parameters here. Comma-separated KEY=VALUE, empty = program defaults.
SHARED_ENV=""      # all three services; the city must match, e.g. CITY_GRID_SIZE=60
DISPATCH_ENV=""    # both dispatch services, e.g. CANDIDATES=5,ALT_ROUTES=3
WEB_ENV=""         # web only, e.g. TIMEOUT_MS=10000,TRACK_INTERVAL_MS=2000
AUTO_MAX_INSTANCES=20  # dispatch-auto max-instances (the project's Cloud Run instance quota)

if [ -z "$PROJECT" ]; then
    echo "No GCP project. Run: gcloud config set project <PROJECT_ID>  (or export PROJECT_ID=...)" >&2
    exit 1
fi

service_url() {
    gcloud run services describe "$1" --region "$REGION" --project "$PROJECT" --format='value(status.url)'
}

# URL of an existing service, empty when it has not been deployed yet
existing_url() {
    service_url "$1" 2>/dev/null || true
}

# Joins the non-empty arguments with commas: env_list A=1 "" B=2 -> A=1,B=2
env_list() {
    local out="" part
    for part in "$@"; do
        if [ -n "$part" ]; then
            out="${out:+${out},}${part}"
        fi
    done
    printf '%s' "$out"
}

deploy_service() {
    # $1 name, $2 env (KEY=VALUE,...), $3 max-instances, $4 concurrency, $5 timeout, $6.. extra flags
    local name="$1" env="$2" max="$3" conc="$4" timeout="$5"
    shift 5
    # min-instances is deliberately not passed: redeploying keeps whatever warm/cool set.
    gcloud run deploy "$name" \
        --project "$PROJECT" --region "$REGION" \
        --image "$IMAGE" \
        --service-account "$RUNTIME_SA" \
        --set-env-vars "$env" \
        --cpu 1 --memory 512Mi \
        --concurrency "$conc" --timeout "$timeout" \
        --max-instances "$max" \
        --allow-unauthenticated \
        "$@"
}

cmd_deploy() {
    echo "== Enabling APIs =="
    gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
        iam.googleapis.com --project "$PROJECT"

    # Since 2024, new projects run Cloud Build as the default compute service account, which has
    # no build roles. These roles are for building only: the services run as RUNTIME_SA instead.
    echo "== Build permissions =="
    local number sa
    number="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
    sa="${number}-compute@developer.gserviceaccount.com"
    for role in roles/cloudbuild.builds.builder \
        roles/storage.objectAdmin \
        roles/artifactregistry.writer \
        roles/logging.logWriter; do
        # re-adding an existing binding is a no-op
        gcloud projects add-iam-policy-binding "$PROJECT" \
            --member "serviceAccount:${sa}" \
            --role "$role" \
            --condition=None \
            --quiet >/dev/null
    done

    # Created before the build so IAM has propagated by the time the services are deployed
    echo "== Runtime service account =="
    if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT" >/dev/null 2>&1; then
        gcloud iam service-accounts create "$RUNTIME_SA_NAME" --project "$PROJECT" \
            --display-name "delivery demo runtime (no roles)"
    fi

    echo "== Artifact Registry repo =="
    if ! gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
        gcloud artifacts repositories create "$REPO" --repository-format docker \
            --location "$REGION" --project "$PROJECT"
    fi

    echo "== Building image once =="
    gcloud builds submit --tag "$IMAGE" --project "$PROJECT" .

    # A service's URL does not change once it exists, so on a redeploy the URLs go into the same
    # revision as the new image and there is no window with empty FIXED_URL / ALLOWED_ORIGIN.
    local fixed auto web
    fixed="$(existing_url dispatch-fixed)"
    auto="$(existing_url dispatch-auto)"
    web="$(existing_url web)"

    local dispatch_env web_env
    dispatch_env="$(env_list APP=dispatch "$SHARED_ENV" "$DISPATCH_ENV" "${web:+ALLOWED_ORIGIN=${web}}")"
    web_env="$(env_list APP=web "$SHARED_ENV" "$WEB_ENV" "${fixed:+FIXED_URL=${fixed}}" "${auto:+AUTO_URL=${auto}}")"

    echo "== Deploying services (same image) =="
    deploy_service dispatch-fixed "$dispatch_env" 1 4 30 --cpu-boost
    deploy_service dispatch-auto  "$dispatch_env" "$AUTO_MAX_INSTANCES" 4 30 --cpu-boost
    # 300 s: SSE reconnects every 5 minutes; a longer timeout keeps a replaced web instance
    # alive while the only allowed instance is busy draining, and every request gets 429
    deploy_service web            "$web_env"      1 250 300

    # First deploy only: the URLs did not exist until now
    if [ -z "$fixed" ] || [ -z "$auto" ] || [ -z "$web" ]; then
        echo "== Wiring URLs (first deploy) =="
        fixed="$(service_url dispatch-fixed)"
        auto="$(service_url dispatch-auto)"
        web="$(service_url web)"
        gcloud run services update dispatch-fixed --region "$REGION" --project "$PROJECT" \
            --update-env-vars "ALLOWED_ORIGIN=${web}"
        gcloud run services update dispatch-auto --region "$REGION" --project "$PROJECT" \
            --update-env-vars "ALLOWED_ORIGIN=${web}"
        gcloud run services update web --region "$REGION" --project "$PROJECT" \
            --update-env-vars "FIXED_URL=${fixed},AUTO_URL=${auto}"
    fi

    echo
    echo "Done."
    echo "  web:            ${web}"
    echo "  dispatch-fixed: ${fixed}"
    echo "  dispatch-auto:  ${auto}"
    echo "Digests (the two dispatch services must match):"
    for s in dispatch-fixed dispatch-auto; do
        gcloud run services describe "$s" --region "$REGION" --project "$PROJECT" \
            --format='value(spec.template.spec.containers[0].image)'
    done
}

cmd_scale() {
    local min="$1" s
    for s in "${SERVICES[@]}"; do
        gcloud run services update "$s" --region "$REGION" --project "$PROJECT" --min-instances "$min"
    done
}

cmd_budget() {
    : "${BILLING_ACCOUNT:?Set BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX (gcloud billing accounts list)}"
    gcloud billing budgets create \
        --billing-account "$BILLING_ACCOUNT" \
        --display-name "delivery-demo-1usd" \
        --budget-amount 1USD \
        --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0
}

case "${1:-}" in
    deploy) cmd_deploy ;;
    warm)   cmd_scale 1 ;;
    cool)   cmd_scale 0 ;;
    budget) cmd_budget ;;
    urls)   for s in "${SERVICES[@]}"; do echo "$s: $(service_url "$s")"; done ;;
    *) echo "Usage: $0 {deploy|warm|cool|budget|urls}" >&2; exit 1 ;;
esac
