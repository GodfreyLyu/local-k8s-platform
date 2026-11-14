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
    application = object({
      namespace = string
    })
    derived = object({
      kafka_name           = string
      validation_namespace = string
      labels               = map(string)
    })
    kafka = object({
      namespace      = string
      storage        = string
      topics         = list(string)
      consumer_group = string
      resources = object({
        requests = map(string)
        limits   = map(string)
      })
    })
    monitoring = object({
      enabled    = bool
      namespace  = string
      pod_labels = map(string)
    })
    network = object({
      api_server_cidrs = list(string)
      dns_namespace    = string
      dns_labels       = map(string)
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
