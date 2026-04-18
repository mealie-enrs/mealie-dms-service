# DMS — Deployment Instructions

Deployment target: **CPU node `192.5.87.45`** (`proj26-mealie-node-cpu`)  
Deployed: 2026-04-18  
Stack: Docker Compose (12 containers)

---

## Quick links

| Service | URL | Credentials |
|---------|-----|-------------|
| **API** | http://192.5.87.45:8000 | — |
| **API health** | http://192.5.87.45:8000/healthz | — |
| **API docs** | http://192.5.87.45:8000/docs | — |
| **Grafana** | http://192.5.87.45:3000 | admin / admin |
| **Prometheus** | http://192.5.87.45:9090 | — |
| **Prometheus targets** | http://192.5.87.45:9090/targets | — |
| **Metabase** | http://192.5.87.45:3001 | set on first login |
| **Qdrant UI** | http://192.5.87.45:6333/dashboard | — |
| **Prefect UI** | http://192.5.87.45:4200 | — |

---

## Infrastructure

| Node | IP | Role |
|------|----|------|
| `proj26-mealie-node-cpu` | `192.5.87.45` | DMS (this service) |
| `proj26-mealie-node-gpu` | `192.5.87.188` | model-serve (separate) |

SSH key: `~/.ssh/id_rsa_chameleon`, user: `cc`

---

## Services & ports

| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| `dms-api` | `dms-app:local` | `8000` | FastAPI HTTP control plane |
| `dms-worker` | `dms-app:local` | — | Celery async workers |
| `dms-scheduler` | `dms-app:local` | — | Celery beat scheduler |
| `dms-postgres` | `postgres:16-alpine` | `5432` | Metadata database |
| `dms-redis` | `redis:7-alpine` | `6379` | Broker + result backend |
| `dms-metabase` | `metabase/metabase:v0.56.3` | `3001` | BI dashboard |
| `dms-prometheus` | `prom/prometheus:v2.51.0` | `9090` | Metrics collection |
| `dms-grafana` | `grafana/grafana:10.4.2` | `3000` | Metrics visualization |
| `dms-node-exporter` | `prom/node-exporter:v1.7.0` | — | Host CPU/mem/disk metrics |
| `dms-cadvisor` | `gcr.io/cadvisor/cadvisor:v0.49.1` | — | Container metrics |
| `dms-redis-exporter` | `oliver006/redis_exporter:v1.58.0` | — | Redis metrics |
| `dms-postgres-exporter` | `prometheuscommunity/postgres-exporter:v0.15.0` | — | Postgres metrics |

---

## Deploy (fresh node or redeploy)

### Step 1 — fill in credentials

Open `deploy.sh` and set the variables at the top:

```
REPO_URL                     GitHub repo URL
POSTGRES_USER/PASSWORD/DB    Postgres credentials
SWIFT_APP_CREDENTIAL_ID      Chameleon app credential ID
SWIFT_APP_CREDENTIAL_SECRET  Chameleon app credential secret
SWIFT_USER_UPLOADS_CONTAINER Swift container for uploads
SWIFT_TRAINING_CONTAINER     Swift container for training data
KAGGLE_USERNAME / KEY        Kaggle API credentials (optional)
```

### Step 2 — copy script to server

```bash
scp -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon \
  deploy.sh \
  cc@192.5.87.45:~/deploy.sh
```

### Step 3 — run it

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 "bash deploy.sh"
```

The script will:
1. Install Docker if not present
2. Clone / pull latest code from GitHub
3. Write `.env` from the variables in the script
4. `docker compose up -d --build`
5. Poll `/healthz` until the API responds (120s timeout)
6. Print all service URLs

---

## Status checks

### From your laptop

**API health:**
```bash
curl http://192.5.87.45:8000/healthz
# expected: {"status":"ok"}
```

**All container states:**
```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && docker compose ps"
```

### From inside the node

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45
cd mealie-dms-service
```

**Container status:**
```bash
docker compose ps
```

**Live API logs:**
```bash
docker compose logs -f dms-api
```

**Live worker logs:**
```bash
docker compose logs -f dms-worker
```

**Live scheduler logs:**
```bash
docker compose logs -f dms-scheduler
```

**All logs (all containers):**
```bash
docker compose logs -f
```

**Postgres — connect directly:**
```bash
docker exec -it dms-postgres psql -U dms -d dms
```

**Redis — ping:**
```bash
docker exec -it dms-redis redis-cli ping
# expected: PONG
```

**Check `.env` that was written by deploy.sh:**
```bash
cat ~/mealie-dms-service/.env
```

---

## Redeploy after a code change

Same two commands every time — script pulls latest from GitHub automatically:

```bash
scp -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon \
  deploy.sh cc@192.5.87.45:~/deploy.sh

ssh -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 "bash deploy.sh"
```

---

## Tear down

**Stop stack, keep database volumes:**
```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && bash ~/deploy.sh --down"
```

**Stop stack and wipe all data (Postgres, Metabase):**
```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && bash ~/deploy.sh --destroy"
```

---

## Troubleshooting

**API not responding after deploy:**
```bash
docker compose logs dms-api --tail 50
```

**Worker not picking up jobs:**
```bash
docker compose logs dms-worker --tail 50
```

**Postgres not healthy:**
```bash
docker compose logs postgres --tail 30
docker exec -it dms-postgres pg_isready -U dms -d dms
```

**Restart a single container:**
```bash
docker compose restart dms-api
docker compose restart dms-worker
```

**Full restart of the stack:**
```bash
docker compose down && docker compose up -d
```

**Disk space (if build gets stuck):**
```bash
df -h /
docker system df
docker system prune -f   # remove unused images/containers to free space
```

---

## Service URLs (live)

| Service | URL | Credentials |
|---------|-----|-------------|
| API | http://192.5.87.45:8000 | — |
| API health | http://192.5.87.45:8000/healthz | — |
| API docs | http://192.5.87.45:8000/docs | — |
| Metabase | http://192.5.87.45:3001 | set on first login |
| Grafana | http://192.5.87.45:3000 | admin / admin (change after first login) |
| Prometheus | http://192.5.87.45:9090 | — |

## Grafana dashboards (import after first login)

1. Open http://192.5.87.45:3000 → login → **Dashboards → Import**
2. Import these community dashboards by ID:

| Dashboard | ID | What it shows |
|-----------|-----|--------------|
| Node Exporter Full | `1860` | Host CPU, memory, disk, network |
| Docker containers (cAdvisor) | `14282` | Per-container CPU, memory, network |
| Redis | `763` | Redis ops, memory, connections |
| Postgres | `9628` | Postgres queries, connections, cache |

Prometheus datasource is pre-configured automatically — just select it when importing.
