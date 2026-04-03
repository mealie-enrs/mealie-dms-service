output "floating_ip" {
  value       = openstack_networking_floatingip_v2.dms_fip.address
  description = "Public IP for SSH and API"
}

output "ssh_command" {
  value       = "ssh ${var.ssh_user}@${openstack_networking_floatingip_v2.dms_fip.address}"
  description = "SSH command"
}

output "kubeconfig_copy_command" {
  value       = "scp ${var.ssh_user}@${openstack_networking_floatingip_v2.dms_fip.address}:/etc/rancher/k3s/k3s.yaml ~/.kube/dms-k3s.yaml"
  description = "Copy kubeconfig from VM"
}
