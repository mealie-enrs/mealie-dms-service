data "openstack_networking_network_v2" "private" {
  name = var.network_name
}

locals {
  scheduler_hints = var.reservation_id != null && var.reservation_id != "" ? {
    reservation = var.reservation_id
  } : {}
}

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

resource "openstack_networking_secgroup_rule_v2" "egress" {
  direction         = "egress"
  ethertype         = "IPv4"
  security_group_id = openstack_networking_secgroup_v2.dms.id
}

resource "openstack_compute_instance_v2" "dms" {
  name        = var.instance_name
  image_name  = var.image_name
  flavor_name = var.flavor_name
  key_pair    = var.ssh_key_name
  scheduler_hints = local.scheduler_hints

  network {
    uuid = data.openstack_networking_network_v2.private.id
  }

  security_groups = [openstack_networking_secgroup_v2.dms.name]

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    ssh_user = var.ssh_user
  })
}

resource "openstack_blockstorage_volume_v3" "dms_data" {
  name = "${var.instance_name}-data"
  size = var.volume_size_gb
}

resource "openstack_compute_volume_attach_v2" "attach_dms_data" {
  instance_id = openstack_compute_instance_v2.dms.id
  volume_id   = openstack_blockstorage_volume_v3.dms_data.id
}

resource "openstack_networking_floatingip_v2" "dms_fip" {
  pool = var.external_network_name
}

resource "openstack_compute_floatingip_associate_v2" "fip_assoc" {
  floating_ip = openstack_networking_floatingip_v2.dms_fip.address
  instance_id = openstack_compute_instance_v2.dms.id
}
