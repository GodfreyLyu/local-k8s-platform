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
    cluster = object({
      context    = string
      kubeconfig = string
    })
    application = object({
      database  = string
      namespace = string
      username  = string
    })
    derived = object({
      namespace            = string
      validation_namespace = string
      postgres_name        = string
      labels               = map(string)
    })
    monitoring = object({
      enabled    = bool
      namespace  = string
      pod_labels = map(string)
    })
    postgres = object({
      storage = string
      resources = object({
        requests = map(string)
        limits   = map(string)
      })
    })
    storage = object({
      class = string
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
