terraform {
  required_version = ">= 1.6.0"

  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 1.54"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

provider "openstack" {
  auth_url                      = var.os_auth_url
  region                        = var.os_region_name
  tenant_name                   = var.os_project_name
  user_name                     = var.os_username
  password                      = var.os_password
  application_credential_id     = var.os_application_credential_id
  application_credential_secret = var.os_application_credential_secret
}
