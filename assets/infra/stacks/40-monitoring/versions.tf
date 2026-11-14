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
      database = string
    })
    derived = object({
      namespace     = string
      postgres_name = string
      kafka_name    = string
      labels        = map(string)
    })
    kafka = object({
      enabled   = bool
      namespace = string
    })
    monitoring = object({
      namespace = string
      release   = string
    })
    postgres = object({
      enabled = bool
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
