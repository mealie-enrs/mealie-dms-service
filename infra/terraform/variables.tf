variable "os_auth_url" {
  type        = string
  description = "OpenStack auth URL"
}

variable "os_region_name" {
  type        = string
  description = "OpenStack region name"
}

variable "os_project_name" {
  type        = string
  description = "OpenStack project name"
}

variable "os_username" {
  type        = string
  description = "OpenStack username (for password auth)"
  default     = null
}

variable "os_password" {
  type        = string
  description = "OpenStack password (for password auth)"
  default     = null
  sensitive   = true
}

variable "os_application_credential_id" {
  type        = string
  description = "OpenStack application credential ID"
  default     = null
}

variable "os_application_credential_secret" {
  type        = string
  description = "OpenStack application credential secret"
  default     = null
  sensitive   = true
}

variable "instance_name" {
  type        = string
  description = "VM name"
  default     = "proj26-dms-k3s"
}

variable "image_name" {
  type        = string
  description = "Image name, e.g. CC-Ubuntu22.04"
}

variable "flavor_name" {
  type        = string
  description = "Flavor name, e.g. m1.large"
}

variable "network_name" {
  type        = string
  description = "Private network name"
}

variable "external_network_name" {
  type        = string
  description = "External network/pool name for floating IPs"
  default     = "public"
}

variable "ssh_key_name" {
  type        = string
  description = "Existing OpenStack keypair name"
}

variable "reservation_id" {
  type        = string
  description = "Blazar reservation UUID for lease-backed scheduling"
  default     = null
}

variable "allowed_ssh_cidr" {
  type        = string
  description = "CIDR allowed to SSH"
  default     = "0.0.0.0/0"
}

variable "allowed_api_cidr" {
  type        = string
  description = "CIDR allowed to reach DMS API NodePort"
  default     = "0.0.0.0/0"
}

variable "volume_size_gb" {
  type        = number
  description = "Persistent volume size in GB"
  default     = 100
}

variable "ssh_user" {
  type        = string
  description = "Default SSH user for image"
  default     = "cc"
}
