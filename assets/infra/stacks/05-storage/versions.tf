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
  type = object({
    environment_id = string
    cluster = object({
      context    = string
      kubeconfig = string
    })
    budget = object({
      min_disk_free_mib = number
    })
    derived = object({
      labels = map(string)
    })
    storage = object({
      class = string
      nodes = list(string)
    })
  })
}

locals {
  e = var.environment
}

provider "kubernetes" {
  config_path    = local.e.cluster.kubeconfig
  config_context = local.e.cluster.context
}
