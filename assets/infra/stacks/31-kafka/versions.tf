# Purpose: Declare the pinned provider and consume the validated environment contract.
terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    kubernetes = { source = "hashicorp/kubernetes", version = "= 2.38.0" }
  }
  backend "local" {}
}

variable "environment" {
  description = "Environment normalized and validated by the Python entry point."
  type        = any
}

locals {
  e = var.environment
}

provider "kubernetes" {
  config_path    = local.e.cluster.kubeconfig
  config_context = local.e.cluster.context
}
