# Chameleon Deployment Command Log (proj26)

This file captures the full command flow we used, plus the errors encountered and how they were fixed.

## 0) Goal

Deploy DMS infra on Chameleon using a lease in project `proj26`, with OpenStack CLI, and later Terraform/Kubernetes.

---

## 1) OpenStack auth setup

Use the app credential env script:

```bash
source "/Users/mudrex/Desktop/mealie/secrets/app-cred-nidhish-mac-openrc.sh"
openstack token issue
```

If using `clouds.yaml`:

```bash
mv ~/Downloads/clouds.yaml ~/.config/openstack/clouds.yaml
export OS_CLOUD="CHI-251409"
openstack token issue
```

---

## 2) Python/OpenStack CLI environment setup

We discovered plugin mismatch issues between Homebrew `openstack` and Python env packages, so we created a dedicated venv:

```bash
python3 -m venv ~/.venvs/oscli
source ~/.venvs/oscli/bin/activate
pip install -U pip
pip install python-openstackclient python-blazarclient
```

Activate/deactivate commands used:

```bash
# activate oscli
source ~/.venvs/oscli/bin/activate

# deactivate conda ml env
conda deactivate

# deactivate oscli venv
deactivate
```

---

## 3) Lease command checks

Initial commands tried:

```bash
openstack lease list
openstack reservation lease list
```

Expected/working command (after plugin is correctly loaded):

```bash
openstack reservation lease list
openstack reservation lease show <LEASE_ID_OR_NAME>
```

For your lease:

```bash
openstack reservation lease show 39e26c5f-fdd6-4064-9266-07020c09bfd0
```

---

## 4) Reservation extraction (for server scheduler hint)

We used:

```bash
openstack reservation lease show 39e26c5f-fdd6-4064-9266-07020c09bfd0 -f json | jq '.reservations'
```

From output, reservation ID was:

```text
fbf57b65-d92e-420e-a96d-5868360d5748
```

This is the value passed to:

```bash
--hint reservation=<RESERVATION_ID>
```

---

## 5) Keypair and flavor validation

Keypair selected:

```text
nidhish-mac
```

Flavor check command:

```bash
openstack flavor list
```

Result used:

```text
baremetal
```

Image/network discovery commands if needed:

```bash
openstack image list | rg -i "ubuntu|cc-ubuntu"
openstack network list
openstack keypair list
```

---

## 6) Final server create command used

```bash
openstack server create \
  --flavor baremetal \
  --image CC-Ubuntu22.04 \
  --network sharednet1 \
  --key-name nidhish-mac \
  --hint reservation=fbf57b65-d92e-420e-a96d-5868360d5748 \
  proj26-dms-k3s
```

Check status:

```bash
openstack server show proj26-dms-k3s -c status -c addresses -c id
```

---

## 7) Floating IP steps

```bash
openstack floating ip create public
openstack floating ip list
openstack server add floating ip proj26-dms-k3s <FLOATING_IP>
```

SSH to VM:

```bash
ssh cc@<FLOATING_IP>
```

---

## 8) Terraform commands used

From `infra/terraform`:

```bash
cp terraform.tfvars.example terraform.tfvars
# edit values: credentials, project, keypair, CIDRs
terraform init
terraform plan -out tfplan
terraform apply tfplan
```

Important variables discussed:

- `os_application_credential_id`
- `os_application_credential_secret`
- `allowed_ssh_cidr`
- `allowed_api_cidr`
- `ssh_key_name`
- `image_name`
- `flavor_name`
- `network_name`
- `instance_name` (set to `proj26-dms-k3s` by default)

---

## 9) Errors encountered and fixes

### Error 1: missing RC file path

Observed:

```text
source: no such file or directory: /Users/mudrex/Desktop/mealie/app-cred-nidhish-mac-openrc.sh
```

Fix:

- Actual file moved to `secrets/`.
- Use:

```bash
source "/Users/mudrex/Desktop/mealie/secrets/app-cred-nidhish-mac-openrc.sh"
```

### Error 2: `openstack lease list` not recognized

Observed:

```text
openstack: 'lease list' is not an openstack command
```

Cause:

- Blazar (reservation/lease) client plugin not loaded in active CLI environment.

Fix:

```bash
pip install python-openstackclient python-blazarclient
```

Then use:

```bash
openstack reservation lease list
```

### Error 3: `openstack reservation lease list` still not recognized

Observed even after package install.

Cause:

- `openstack` binary came from Homebrew (`/opt/homebrew/bin/openstack`), while plugin install happened in a different Python environment.

Fix:

- Create and use dedicated `oscli` venv so CLI and plugins are in same interpreter:

```bash
python3 -m venv ~/.venvs/oscli
source ~/.venvs/oscli/bin/activate
pip install python-openstackclient python-blazarclient
```

### Error 4: wrong lease name

Observed:

```text
Unable to find resource with name 'proj26 deploy'
```

Cause:

- Actual lease name was `proj26`.

Fix:

- Use lease ID directly:

```bash
openstack reservation lease show 39e26c5f-fdd6-4064-9266-07020c09bfd0
```

### Error 5: flavor confusion (`compute_cascadelake_r`)

Cause:

- `compute_cascadelake_r` came from lease `resource_properties` (node type), not from OpenStack flavor catalog.

Fix:

- Use real flavor from `openstack flavor list` (`baremetal` in your project).
- Keep reservation routing via:

```bash
--hint reservation=fbf57b65-d92e-420e-a96d-5868360d5748
```

### Warning: readline/tab completion message

Observed:

```text
Readline features including tab completion have been disabled...
```

Impact:

- Non-blocking warning; commands still run.

Fix (optional):

```bash
pip install gnureadline
```

---

## 10) Security notes

- Do not commit credential files (`openrc`, app credential secret, tfvars with secrets).
- Rotate credentials if exposed.
- Keep `allowed_ssh_cidr` and `allowed_api_cidr` narrow (`x.x.x.x/32`), avoid `0.0.0.0/0`.

---

## 11) Floating IP/SSH issues encountered (and exact fixes)

This section documents the exact IP/connectivity path we saw and how it was resolved.

### Symptom A: Floating IP was created but showed `DOWN`

Observed immediately after allocation:

```bash
openstack floating ip create public
```

Output included:

- `floating_ip_address = 192.5.87.178`
- `status = DOWN`
- `port_id = None`

Why:

- This is expected before attaching the floating IP to a server port.

Fix:

```bash
openstack server add floating ip proj26-dms-k3s 192.5.87.178
```

Verify attachment:

```bash
openstack floating ip show 192.5.87.178 -c status -c fixed_ip_address -c port_id
openstack server show proj26-dms-k3s -c addresses -c status
```

Expected after fix:

- floating IP `status = ACTIVE`
- `fixed_ip_address = 10.140.82.8`
- server address list contains both fixed + floating IPs

### Symptom B: server in `BUILD` and no address

Observed:

- server `status = BUILD`
- empty `addresses`

Why:

- Instance provisioning was not complete yet.

Fix:

- Wait until server is `ACTIVE`:

```bash
openstack server show proj26-dms-k3s -c status -c addresses -c fault
```

### Symptom C: initial SSH failure (`Network is unreachable`)

Observed:

```text
ssh: connect to host 192.5.87.178 port 22: Network is unreachable
```

Diagnostics used:

```bash
route -n get 192.5.87.178
ping -c 3 192.5.87.178
traceroute 192.5.87.178
```

Server-side checks:

```bash
openstack server show proj26-dms-k3s -c security_groups -f yaml
openstack security group rule list default
```

What changed:

- Connectivity path and floating-IP binding became healthy.
- SSH reached host and moved to key authentication phase.

### Symptom D: SSH key rejected (`Permission denied (publickey)`)

Observed:

```text
cc@192.5.87.178: Permission denied (publickey).
```

Cause:

- SSH was offering keys that did not match the OpenStack keypair bound to the instance.

Fix:

- Use the matching private key explicitly with `IdentitiesOnly`:

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon cc@192.5.87.178
```

This command succeeded.

### Final connect command (working)

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_rsa_chameleon cc@192.5.87.178
```

---

## 12) DMS Layer Recipe1M tiny sample runbook

### Stage sample files to object storage

```bash
cd /Users/mudrex/Desktop/mealie
python scripts/ingest_recipe1m_sample.py \
  --manifest-source "<jsonl-file-or-url>" \
  --sample-size 1000 \
  --container proj26-obj-store \
  --raw-prefix raw/recipe1m/v1
```

### Trigger async ingest + auto-publish `v1`

```bash
curl -X POST "http://192.5.87.69:30080/datasets/<dataset_id>/ingest/recipe1m" \
  -H "Content-Type: application/json" \
  -d '{
    "manifest_source": "<jsonl-file-or-url>",
    "sample_size": 1000,
    "raw_prefix": "raw/recipe1m",
    "target_container": "proj26-obj-store",
    "auto_publish_version": "v1"
  }'
```

### Check job status

```bash
curl "http://192.5.87.69:30080/jobs/<job_id>"
```

### Expected artifacts in object store

```text
proj26-obj-store/
  raw/recipe1m/<run-id>/...
  raw/recipe1m/<run-id>/ingest_summary.json
  objects/sha256/ab/cd/<hash>.jpg
  recipe1m_versions/v1/manifest.parquet
  recipe1m_versions/v1/meta.json
```
