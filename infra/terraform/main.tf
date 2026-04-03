resource "openstack_networking_secgroup_v2" "dms" {
  name        = "${var.instance_name}-sg"
  description = "DMS K3s security group"
}

resource "openstack_networking_secgroup_rule_v2" "ssh" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = var.allowed_ssh_cidr
  security_group_id = openstack_networking_secgroup_v2.dms.id
}

resource "openstack_networking_secgroup_rule_v2" "api" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 30080
  port_range_max    = 30080
  remote_ip_prefix  = var.allowed_api_cidr
  security_group_id = openstack_networking_secgroup_v2.dms.id
}

resource "openstack_networking_floatingip_v2" "dms_fip" {
  pool = var.external_network_name
}

resource "null_resource" "create_reserved_server" {
  depends_on = [
    openstack_networking_secgroup_v2.dms,
    openstack_networking_secgroup_rule_v2.ssh,
    openstack_networking_secgroup_rule_v2.api,
  ]

  triggers = {
    instance_name   = var.instance_name
    image_name      = var.image_name
    flavor_name     = var.flavor_name
    network_name    = var.network_name
    key_name        = var.ssh_key_name
    reservation_id  = var.reservation_id
    security_group  = openstack_networking_secgroup_v2.dms.name
    cloud_init_hash = sha1(templatefile("${path.module}/cloud-init.yaml.tftpl", { ssh_user = var.ssh_user }))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      if openstack server show "${var.instance_name}" >/dev/null 2>&1; then
        echo "Server ${var.instance_name} already exists; skipping create."
      else
        cat > /tmp/${var.instance_name}-cloud-init.yaml <<'EOF'
${templatefile("${path.module}/cloud-init.yaml.tftpl", { ssh_user = var.ssh_user })}
EOF
        openstack server create \
          --flavor "${var.flavor_name}" \
          --image "${var.image_name}" \
          --network "${var.network_name}" \
          --key-name "${var.ssh_key_name}" \
          --security-group "${openstack_networking_secgroup_v2.dms.name}" \
          --hint "reservation=${var.reservation_id}" \
          --user-data "/tmp/${var.instance_name}-cloud-init.yaml" \
          "${var.instance_name}"
      fi

      for i in $(seq 1 90); do
        STATUS="$(openstack server show "${var.instance_name}" -f value -c status || true)"
        if [ "$STATUS" = "ACTIVE" ]; then
          exit 0
        fi
        if [ "$STATUS" = "ERROR" ]; then
          echo "Server reached ERROR state"
          openstack server show "${var.instance_name}" -f yaml
          exit 1
        fi
        sleep 10
      done

      echo "Timed out waiting for ${var.instance_name} to become ACTIVE"
      exit 1
    EOT
  }
}

resource "null_resource" "associate_fip" {
  depends_on = [
    null_resource.create_reserved_server,
    openstack_networking_floatingip_v2.dms_fip,
  ]

  triggers = {
    instance_name = var.instance_name
    floating_ip   = openstack_networking_floatingip_v2.dms_fip.address
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      PORT_ID="$(openstack floating ip show "${openstack_networking_floatingip_v2.dms_fip.address}" -f value -c port_id || true)"
      if [ -n "$PORT_ID" ] && [ "$PORT_ID" != "None" ]; then
        echo "Floating IP ${openstack_networking_floatingip_v2.dms_fip.address} already associated."
      else
        openstack server add floating ip "${var.instance_name}" "${openstack_networking_floatingip_v2.dms_fip.address}"
      fi
    EOT
  }
}
