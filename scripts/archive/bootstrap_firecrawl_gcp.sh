#!/usr/bin/env bash
# Bootstrap a self-hosted Firecrawl instance on GCP using Docker Compose.
# Idempotent-ish: safe to re-run after fixing failures.
#
# Usage:
#   bash scripts/bootstrap_firecrawl_gcp.sh
#
# Prerequisites:
#   - gcloud CLI installed and authenticated (gcloud auth login)
#   - A billing account linked to the project (gcloud beta billing accounts list)
#   - OpenAI API key ready for judging
#
# Env vars (override defaults):
#   GCP_PROJECT         default: costfinder-firecrawl
#   GCP_ZONE            default: us-east1-b
#   GCP_MACHINE_TYPE    default: e2-standard-4
#   VM_NAME             default: firecrawl
#   FIRECRAWL_VERSION   default: v2.0.0 (must include PR #3470)
#   OPENAI_API_KEY      required (for judging)

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:-costfinder-firecrawl}"
GCP_ZONE="${GCP_ZONE:-us-east1-b}"
GCP_MACHINE_TYPE="${GCP_MACHINE_TYPE:-e2-standard-4}"
VM_NAME="${VM_NAME:-firecrawl}"
FIRECRAWL_VERSION="${FIRECRAWL_VERSION:-v2.0.0}"
DISK_SIZE="${DISK_SIZE:-50}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"

if [[ -z "$OPENAI_API_KEY" ]]; then
  echo "ERROR: OPENAI_API_KEY env var required for judging LLM"
  exit 1
fi

echo "==> Setting project: $GCP_PROJECT"
gcloud config set project "$GCP_PROJECT"

echo "==> Enabling Compute Engine API"
gcloud services enable compute.googleapis.com --project="$GCP_PROJECT" 2>/dev/null || true

echo "==> Creating VM $VM_NAME ($GCP_MACHINE_TYPE in $GCP_ZONE)"
if ! gcloud compute instances describe "$VM_NAME" --zone="$GCP_ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$VM_NAME" \
    --zone="$GCP_ZONE" \
    --machine-type="$GCP_MACHINE_TYPE" \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size="${DISK_SIZE}GB" \
    --boot-disk-type=pd-ssd \
    --tags=firecrawl,ssh-allowed
else
  echo "    VM already exists, skipping creation"
fi

echo "==> Configuring firewall (limit to your IP)"
MY_IP=$(curl -s https://api.ipify.org)
echo "    Your public IP: $MY_IP"

if ! gcloud compute firewall-rules describe allow-firecrawl-3002 >/dev/null 2>&1; then
  gcloud compute firewall-rules create allow-firecrawl-3002 \
    --network=default \
    --action=allow \
    --rules=tcp:3002 \
    --source-ranges="${MY_IP}/32" \
    --target-tags=firecrawl
else
  echo "    Firewall rule allow-firecrawl-3002 already exists"
fi

if ! gcloud compute firewall-rules describe allow-ssh-firecrawl >/dev/null 2>&1; then
  gcloud compute firewall-rules create allow-ssh-firecrawl \
    --network=default \
    --action=allow \
    --rules=tcp:22 \
    --source-ranges="${MY_IP}/32" \
    --target-tags=firecrawl
else
  echo "    Firewall rule allow-ssh-firecrawl already exists"
fi

VM_EXTERNAL_IP=$(gcloud compute instances describe "$VM_NAME" --zone="$GCP_ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
echo "==> VM external IP: $VM_EXTERNAL_IP"

echo
echo "================================================"
echo "Next steps (SSH into VM and run setup):"
echo
echo "  gcloud compute ssh $VM_NAME --zone=$GCP_ZONE"
echo
echo "Then in the VM, run:"
echo
echo "  sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg lsb-release git"
echo "  sudo install -m 0755 -d /etc/apt/keyrings"
echo "  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg"
echo "  sudo chmod a+r /etc/apt/keyrings/docker.gpg"
echo '  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null'
echo "  sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
echo "  sudo usermod -aG docker \$USER && newgrp docker"
echo "  cd ~ && git clone https://github.com/firecrawl/firecrawl.git && cd firecrawl"
echo "  git checkout $FIRECRAWL_VERSION"
echo "  cp .env.example .env"
echo "  # Edit .env: set OPENAI_API_KEY, PORT=3002, HOST=0.0.0.0, USE_DB_AUTHENTICATION=false"
echo "  # BULL_AUTH_KEY=\$(openssl rand -hex 32)"
echo "  docker compose up -d"
echo "  docker compose ps"
echo
echo "Then point CostFinder-Crawler .env at this instance:"
echo "  FIRECRAWL_API_URL=http://$VM_EXTERNAL_IP:3002"
echo "  FIRECRAWL_API_KEY=  # leave empty if no proxy auth"
echo
echo "Full guide: docs/self_hosted_firecrawl_gcp.md"
echo "================================================"
