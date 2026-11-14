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
      namespace       = string
      service_account = string
    })
    derived = object({
      namespace            = string
      validation_namespace = string
      postgres_name        = string
      kafka_name           = string
      labels               = map(string)
    })
    network = object({
      dns_namespace = string
      dns_labels    = map(string)
    })
    postgres = object({
      enabled = bool
    })
    kafka = object({
      enabled   = bool
      namespace = string
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
