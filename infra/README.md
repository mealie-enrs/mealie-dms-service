# Infra: Terraform + Kubernetes (Chameleon)

This folder contains an infrastructure path that keeps your app code separate:

- `terraform/`: provisions one Chameleon VM, security group, floating IP, and persistent volume
- `k8s/`: deploys DMS runtime roles (`api`, `worker`, `scheduler`) plus `postgres` and `redis`

## 1) Provision infrastructure with Terraform

From `infra/terraform`:

```bash
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars for your project values
terraform init
terraform plan -out tfplan
terraform apply tfplan
```

Required variables are in `terraform/variables.tf`. Use a `terraform.tfvars` file.

For lease-backed provisioning, set `reservation_id` in `terraform.tfvars`.
This Terraform stack uses `local-exec` + OpenStack CLI to create the instance
with `--hint reservation=<UUID>`.

Before `terraform apply`, ensure:

- `openstack` CLI is installed and available in your shell
- auth env is loaded (for example: `source .../app-cred-...-openrc.sh`)

After apply, Terraform outputs:

- VM floating IP
- SSH command
- kubeconfig retrieval command

## 2) Configure kubectl

Copy kubeconfig from VM and rewrite server endpoint to floating IP:

```bash
mkdir -p ~/.kube
scp cc@<floating-ip>:/etc/rancher/k3s/k3s.yaml ~/.kube/dms-k3s.yaml
sed -i '' "s/127.0.0.1/<floating-ip>/g" ~/.kube/dms-k3s.yaml
export KUBECONFIG=~/.kube/dms-k3s.yaml
kubectl get nodes
```

## 3) Build and push app image

Kubernetes needs an image in a registry reachable by the VM.

Example:

```bash
docker build -t ghcr.io/<org>/dms-app:<tag> .
docker push ghcr.io/<org>/dms-app:<tag>
```

Set image tag through Kustomize (no per-file edits):

```bash
cd k8s
kustomize edit set image ghcr.io/your-org/dms-app=ghcr.io/<org>/dms-app:<tag>
cd ..
```

If `kustomize` is not installed locally, replace `ghcr.io/your-org/dms-app:latest`
in `k8s/api.yaml`, `k8s/worker.yaml`, and `k8s/scheduler.yaml` manually.

## 4) Configure secrets

Create secrets from template:

```bash
cp k8s/secrets.example.yaml /tmp/dms-secrets.yaml
# edit values in /tmp/dms-secrets.yaml
kubectl apply -f /tmp/dms-secrets.yaml
```

## 5) Deploy to Kubernetes

```bash
kubectl apply -k k8s
kubectl -n dms get pods
```

API is exposed via NodePort `30080`:

```bash
curl http://<floating-ip>:30080/healthz
```

## 6) Scale later (same infra path)

Increase worker throughput:

```bash
kubectl -n dms scale deployment/dms-worker --replicas=3
```

Increase API replicas:

```bash
kubectl -n dms scale deployment/dms-api --replicas=2
```
