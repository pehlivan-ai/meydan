#!/usr/bin/env bash
# One-time GCP setup for an instance's CD: registry, deployer identity, keyless GitHub auth.
# Idempotent-ish: re-running skips what exists. Run as a project owner:
#
#   PROJECT=my-project REGION=europe-west1 ZONE=europe-west1-b VM=my-vm \
#   VM_SA=my-vm@my-project.iam.gserviceaccount.com GITHUB_REPO=owner/repo \
#   scripts/gcp-setup.sh
#
# What it creates (least privilege; resource-scoped where GCP allows it):
#   - Artifact Registry docker repo + cleanup policy (keep 10, untagged 7d, all 90d)
#   - service account github-deployer
#   - Workload Identity Federation pool "github" + provider: only GITHUB_REPO's main branch,
#     matched by immutable repo and owner ids (a re-created repo with the same name can't use it)
#   - roles: registry writer (deployer) / reader (VM), OS Login admin + viewer on the VM only,
#     act-as the VM's service account, IAP tunnel, and a custom role with compute.projects.get
#     (needed by `gcloud compute ssh`)
set -euo pipefail

: "${PROJECT:?}" "${REGION:?}" "${ZONE:?}" "${VM:?}" "${VM_SA:?}" "${GITHUB_REPO:?}"
REPO_NAME=${REPO_NAME:-$(basename "$GITHUB_REPO")}
POOL=${POOL:-github}
PROVIDER=${PROVIDER:-$REPO_NAME}
DEPLOYER=github-deployer@$PROJECT.iam.gserviceaccount.com
g() { gcloud --project "$PROJECT" "$@"; }
exists() { "$@" >/dev/null 2>&1; }

PROJECT_NUMBER=$(g projects describe "$PROJECT" --format='value(projectNumber)')
REPO_ID=$(gh api "repos/$GITHUB_REPO" --jq .id)
OWNER_ID=$(gh api "repos/$GITHUB_REPO" --jq .owner.id)

echo "apis"
g services enable artifactregistry.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com iam.googleapis.com iap.googleapis.com oslogin.googleapis.com

echo "registry"
exists g artifacts repositories describe "$REPO_NAME" --location "$REGION" ||
  g artifacts repositories create "$REPO_NAME" --repository-format=docker --location "$REGION"
policy=$(mktemp)
cat > "$policy" <<'JSON'
[
  {"name": "keep-recent-10", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 10}},
  {"name": "delete-untagged-after-7d", "action": {"type": "Delete"},
   "condition": {"tagState": "UNTAGGED", "olderThan": "7d"}},
  {"name": "delete-older-than-90d", "action": {"type": "Delete"},
   "condition": {"tagState": "ANY", "olderThan": "90d"}}
]
JSON
g artifacts repositories set-cleanup-policies "$REPO_NAME" --location "$REGION" \
  --policy "$policy" --no-dry-run --format=none
rm -f "$policy"

echo "deployer"
exists g iam service-accounts describe "$DEPLOYER" ||
  g iam service-accounts create github-deployer --display-name "GitHub Actions deployer ($GITHUB_REPO, main)"

echo "workload identity federation"
exists g iam workload-identity-pools describe "$POOL" --location global ||
  g iam workload-identity-pools create "$POOL" --location global --display-name "GitHub Actions"
exists g iam workload-identity-pools providers describe "$PROVIDER" --location global --workload-identity-pool "$POOL" ||
  g iam workload-identity-pools providers create-oidc "$PROVIDER" --location global \
    --workload-identity-pool "$POOL" --display-name "$GITHUB_REPO" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition "assertion.repository_id=='$REPO_ID' && assertion.repository_owner_id=='$OWNER_ID' && assertion.ref=='refs/heads/main'"
g iam service-accounts add-iam-policy-binding "$DEPLOYER" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository_id/$REPO_ID" \
  --format=none

echo "roles"
SA=serviceAccount:$DEPLOYER
g artifacts repositories add-iam-policy-binding "$REPO_NAME" --location "$REGION" --member "$SA" --role roles/artifactregistry.writer --format=none
g artifacts repositories add-iam-policy-binding "$REPO_NAME" --location "$REGION" --member "serviceAccount:$VM_SA" --role roles/artifactregistry.reader --format=none
g compute instances add-iam-policy-binding "$VM" --zone "$ZONE" --member "$SA" --role roles/compute.osAdminLogin --format=none
g compute instances add-iam-policy-binding "$VM" --zone "$ZONE" --member "$SA" --role roles/compute.viewer --format=none
g iam service-accounts add-iam-policy-binding "$VM_SA" --member "$SA" --role roles/iam.serviceAccountUser --format=none
g projects add-iam-policy-binding "$PROJECT" --member "$SA" --role roles/iap.tunnelResourceAccessor --condition=None --format=none
exists g iam roles describe deployProjectRead ||
  g iam roles create deployProjectRead --title "Deploy: read project (gcloud compute ssh)" \
    --permissions compute.projects.get --stage GA --format=none
g projects add-iam-policy-binding "$PROJECT" --member "$SA" --role "projects/$PROJECT/roles/deployProjectRead" --condition=None --format=none

cat <<EOF

Done. Put these in .github/workflows/ci-cd.yml:
  PROJECT: $PROJECT
  IMAGE: $REGION-docker.pkg.dev/$PROJECT/$REPO_NAME/app
  WIF_PROVIDER: projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER
  DEPLOYER: $DEPLOYER
EOF
