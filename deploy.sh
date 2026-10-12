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

if [ -z "$PROJECT" ]; then
    echo "No GCP project. Run: gcloud config set project <PROJECT_ID>  (or export PROJECT_ID=...)" >&2
    exit 1
fi

service_url() {
    gcloud run services describe "$1" --region "$REGION" --project "$PROJECT" --format='value(status.url)'
}

deploy_service() {
    # $1 name, $2 APP, $3 max-instances, $4 concurrency, $5 timeout, $6.. extra flags
    local name="$1" app="$2" max="$3" conc="$4" timeout="$5"
    shift 5
    # min-instances is deliberately not passed: redeploying keeps whatever warm/cool set.
    gcloud run deploy "$name" \
        --project "$PROJECT" --region "$REGION" \
        --image "$IMAGE" \
        --set-env-vars "APP=${app}" \
        --cpu 1 --memory 512Mi \
        --concurrency "$conc" --timeout "$timeout" \
        --max-instances "$max" \
        --allow-unauthenticated \
        "$@"
}

cmd_deploy() {
    echo "== Enabling APIs =="
    gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
        --project "$PROJECT"

    # 2024 Update Cloud build builder IAM bug
    # (new projects: default compute service account has no build roles)
    echo "Build Permissions"
    local number="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
    local sa="${number}-compute@developer.gserviceaccount.com"
    for role in roles/cloudbuild.builds.builder \
        roles/storage.objectAdmin \
        roles/artifactregistry.writer \
        roles/logging.logWriter; do
        # re-adding existing binding
        gcloud projects add-iam-policy-binding "$PROJECT" \
            --member "serviceAccount:${sa}" \
            --role "$role" \
            --condition=None \
            --quiet >/dev/null
    done

    

    echo "== Artifact Registry repo =="
    if ! gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
        gcloud artifacts repositories create "$REPO" --repository-format docker \
            --location "$REGION" --project "$PROJECT"
    fi

    echo "== Building image once =="
    gcloud builds submit --tag "$IMAGE" --project "$PROJECT" .

    echo "== Deploying services (same image) =="
    deploy_service dispatch-fixed dispatch 1 4 30 --cpu-boost
    deploy_service dispatch-auto  dispatch 8 4 30 --cpu-boost
    deploy_service web            web      1 250 3600

    echo "== Wiring URLs =="
    local fixed auto web
    fixed="$(service_url dispatch-fixed)"
    auto="$(service_url dispatch-auto)"
    web="$(service_url web)"

    gcloud run services update dispatch-fixed --region "$REGION" --project "$PROJECT" \
        --update-env-vars "ALLOWED_ORIGIN=${web}"
    gcloud run services update dispatch-auto --region "$REGION" --project "$PROJECT" \
        --update-env-vars "ALLOWED_ORIGIN=${web}"
    gcloud run services update web --region "$REGION" --project "$PROJECT" \
        --update-env-vars "FIXED_URL=${fixed},AUTO_URL=${auto}"

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