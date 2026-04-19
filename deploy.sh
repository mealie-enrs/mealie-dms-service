#!/usr/bin/env bash
# =============================================================================
# DMS deploy script — run this directly on the server
#
# Usage:
#   bash deploy.sh            # fresh deploy or redeploy
#   bash deploy.sh --down     # tear everything down (keeps volumes)
#   bash deploy.sh --destroy  # tear down AND remove volumes (wipes DB)
#
# Copy to server:
#   scp -i ~/.ssh/id_rsa_chameleon deploy.sh cc@192.5.87.45:~/
#   ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 "bash deploy.sh"
# =============================================================================
set -euo pipefail

# =============================================================================
# CONFIGURATION — change these before deploying
# =============================================================================

# --- Repo --------------------------------------------------------------------
REPO_URL="https://github.com/mealie-enrs/mealie-dms-service.git"
REPO_BRANCH="main"
APP_DIR="$HOME/mealie-dms-service"

# --- Postgres (internal to the stack) ----------------------------------------
POSTGRES_USER="dms"
POSTGRES_PASSWORD="dms"
POSTGRES_DB="dms"

# --- Redis -------------------------------------------------------------------
REDIS_PORT="6379"

# --- DMS API -----------------------------------------------------------------
DMS_API_PORT="8000"

# --- Metabase ----------------------------------------------------------------
METABASE_PORT="3001"
METABASE_SITE_NAME="DMS Metabase"

# --- Swift / Chameleon object store ------------------------------------------
SWIFT_AUTH_URL="https://chi.uc.chameleoncloud.org:5000/v3"
SWIFT_APP_CREDENTIAL_ID="2abf5e073b03419a956048c65b8fadd9"
SWIFT_APP_CREDENTIAL_SECRET="VNUi54xp6PWYTa_xnv7LsbClptyDaGtExg8x9c203hpOg-3lNRKv-jDCap64j6QlLwsXZzN2LOmyrx81vFpr-A"
SWIFT_USER_UPLOADS_CONTAINER="proj26-user-uploads"
SWIFT_TRAINING_CONTAINER="proj26-obj-store"
SWIFT_RECIPE1M_PREFIX="recipe1m"

# --- Kaggle (optional — leave blank to skip Kaggle downloads) ----------------
KAGGLE_USERNAME="nidhish1010"
KAGGLE_KEY="2db3bd3d0bc695b5a3fed7906d7bb6d5"
KAGGLE_DOWNLOAD_DIR="/tmp/dms-kaggle-downloads"

# --- Qdrant vector store -----------------------------------------------------
QDRANT_HOST="qdrant"
QDRANT_PORT="6333"
QDRANT_COLLECTION="recipe_features"

# --- Prefect pipeline orchestration ------------------------------------------
PREFECT_API_URL="http://prefect-server:4200/api"

# --- Monitoring --------------------------------------------------------------
GRAFANA_USER="admin"
GRAFANA_PASSWORD="admin"

# --- Soda data quality checks (set to true to enable) -----------------------
ENABLE_SODA_CHECKS="false"
SODA_CONFIGURATION_FILE="quality/soda/configuration.yml"
SODA_CHECKS_FILE="quality/soda/checks.yml"

# --- Iceberg lakehouse (uses same Swift S3 endpoint as object store) ---------
ICEBERG_S3_ENDPOINT="https://chi.uc.chameleoncloud.org:7480"
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set in the environment
# to the Chameleon EC2 credentials before running deploy.sh, or added here.
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-}"

# --- Airflow -----------------------------------------------------------------
AIRFLOW_ADMIN_PASSWORD="admin"
AIRFLOW_SECRET_KEY="dms-airflow-secret-$(hostname -s)"

# =============================================================================
# SCRIPT — do not edit below this line
# =============================================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[deploy]${NC} $*"; }
ok()   { echo -e "${GREEN}[  ok  ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ warn ]${NC} $*"; }
die()  { echo -e "${RED}[ fail ]${NC} $*" >&2; exit 1; }

# --- flags -------------------------------------------------------------------
MODE="up"
for arg in "$@"; do
  case "$arg" in
    --down)    MODE="down" ;;
    --destroy) MODE="destroy" ;;
  esac
done

# --- teardown paths ----------------------------------------------------------
if [[ "$MODE" == "down" || "$MODE" == "destroy" ]]; then
  log "Stopping DMS stack in $APP_DIR ..."
  cd "$APP_DIR"
  if [[ "$MODE" == "destroy" ]]; then
    warn "Removing all volumes — this wipes Postgres data."
    docker compose down -v
  else
    docker compose down
  fi
  ok "Done."
  exit 0
fi

# --- 1. Docker ---------------------------------------------------------------
log "Checking Docker ..."
if ! command -v docker &>/dev/null; then
  log "Docker not found — installing ..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  # apply group without logout — use absolute path so exec can find the script
  exec sg docker "bash $(realpath "$0") $*"
fi
docker info &>/dev/null || die "Docker daemon not running. Start it with: sudo systemctl start docker"
ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"

# --- 2. Code -----------------------------------------------------------------
log "Setting up repo at $APP_DIR ..."
if [[ -d "$APP_DIR/.git" ]]; then
  log "Repo exists — pulling latest from $REPO_BRANCH ..."
  git -C "$APP_DIR" fetch origin
  git -C "$APP_DIR" checkout "$REPO_BRANCH"
  git -C "$APP_DIR" pull origin "$REPO_BRANCH"
else
  log "Cloning $REPO_URL ..."
  git clone --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi
ok "Code at $APP_DIR (branch: $REPO_BRANCH)"

cd "$APP_DIR"

# --- 3. Write .env -----------------------------------------------------------
log "Writing .env ..."
cat > .env <<EOF
# Generated by deploy.sh — $(date -u '+%Y-%m-%d %H:%M:%S UTC')
# Re-run deploy.sh to regenerate.

# Postgres
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=${POSTGRES_DB}

# Swift / Chameleon object store
SWIFT_AUTH_URL=${SWIFT_AUTH_URL}
SWIFT_APP_CREDENTIAL_ID=${SWIFT_APP_CREDENTIAL_ID}
SWIFT_APP_CREDENTIAL_SECRET=${SWIFT_APP_CREDENTIAL_SECRET}
SWIFT_USER_UPLOADS_CONTAINER=${SWIFT_USER_UPLOADS_CONTAINER}
SWIFT_TRAINING_CONTAINER=${SWIFT_TRAINING_CONTAINER}
SWIFT_RECIPE1M_PREFIX=${SWIFT_RECIPE1M_PREFIX}

# Kaggle
KAGGLE_USERNAME=${KAGGLE_USERNAME}
KAGGLE_KEY=${KAGGLE_KEY}
KAGGLE_DOWNLOAD_DIR=${KAGGLE_DOWNLOAD_DIR}

# Soda data quality
ENABLE_SODA_CHECKS=${ENABLE_SODA_CHECKS}
SODA_CONFIGURATION_FILE=${SODA_CONFIGURATION_FILE}
SODA_CHECKS_FILE=${SODA_CHECKS_FILE}

# Qdrant
QDRANT_HOST=${QDRANT_HOST}
QDRANT_PORT=${QDRANT_PORT}
QDRANT_COLLECTION=${QDRANT_COLLECTION}

# Prefect
PREFECT_API_URL=${PREFECT_API_URL}

# Iceberg
ICEBERG_S3_ENDPOINT=${ICEBERG_S3_ENDPOINT}
AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}

# Airflow
AIRFLOW_ADMIN_PASSWORD=${AIRFLOW_ADMIN_PASSWORD}
AIRFLOW_SECRET_KEY=${AIRFLOW_SECRET_KEY}

# Monitoring
GRAFANA_USER=${GRAFANA_USER}
GRAFANA_PASSWORD=${GRAFANA_PASSWORD}

# Ports
DMS_API_PORT=${DMS_API_PORT}
METABASE_PORT=${METABASE_PORT}
METABASE_SITE_NAME=${METABASE_SITE_NAME}
EOF
ok ".env written"

# --- 4. Validate required secrets --------------------------------------------
log "Validating required config ..."
MISSING=0
if [[ -z "$SWIFT_APP_CREDENTIAL_ID" ]]; then
  warn "SWIFT_APP_CREDENTIAL_ID is not set — Swift uploads will fail"
  MISSING=1
fi
if [[ -z "$SWIFT_APP_CREDENTIAL_SECRET" ]]; then
  warn "SWIFT_APP_CREDENTIAL_SECRET is not set — Swift uploads will fail"
  MISSING=1
fi
if [[ $MISSING -eq 0 ]]; then
  ok "All required secrets present"
fi

# --- 5. Build + start --------------------------------------------------------
log "Building and starting DMS stack ..."
docker compose pull --ignore-buildable 2>/dev/null || true
docker compose up -d --build

ok "Stack started"

# --- 6. Health checks --------------------------------------------------------
log "Waiting for services to be healthy ..."
TIMEOUT=120
ELAPSED=0
until curl -sf "http://localhost:${DMS_API_PORT}/healthz" &>/dev/null; do
  if [[ $ELAPSED -ge $TIMEOUT ]]; then
    die "API did not become healthy within ${TIMEOUT}s. Check logs: docker compose logs dms-api"
  fi
  sleep 5
  ELAPSED=$((ELAPSED + 5))
  log "  waiting ... ${ELAPSED}s / ${TIMEOUT}s"
done
ok "API healthy at http://localhost:${DMS_API_PORT}"

# --- 7. Summary --------------------------------------------------------------
HOST_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  DMS stack is up${NC}"
echo -e "${GREEN}============================================================${NC}"
  echo -e "  API            →  http://${HOST_IP}:${DMS_API_PORT}"
  echo -e "  API health     →  http://${HOST_IP}:${DMS_API_PORT}/healthz"
  echo -e "  Metabase       →  http://${HOST_IP}:${METABASE_PORT}"
  echo -e "  Qdrant         →  http://${HOST_IP}:6333"
  echo -e "  Prefect        →  http://${HOST_IP}:4200"
  echo -e "  Redpanda       →  kafka://${HOST_IP}:19092  (external)"
  echo -e "  Redpanda UI    →  http://${HOST_IP}:8080"
  echo -e "  Airflow        →  http://${HOST_IP}:8085  (admin / ${AIRFLOW_ADMIN_PASSWORD})"
  echo -e "  Grafana        →  http://${HOST_IP}:3000  (${GRAFANA_USER} / ${GRAFANA_PASSWORD})"
  echo -e "  Prometheus     →  http://${HOST_IP}:9090"
  echo -e "  Postgres       →  ${HOST_IP}:5432  (user: ${POSTGRES_USER})"
  echo -e "  Redis          →  ${HOST_IP}:${REDIS_PORT}"
echo ""
echo -e "  Useful commands:"
echo -e "    docker compose ps"
echo -e "    docker compose logs -f dms-api"
echo -e "    docker compose logs -f dms-worker"
echo -e "    bash deploy.sh --down      # stop (keep data)"
echo -e "    bash deploy.sh --destroy   # stop + wipe DB"
echo -e "${GREEN}============================================================${NC}"
