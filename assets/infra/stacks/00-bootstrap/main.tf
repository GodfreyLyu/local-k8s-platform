# Purpose: Create environment namespaces, application identity, and explicit data-plane isolation.
locals {
  namespaces        = toset([local.e.application.namespace, local.e.derived.namespace, local.e.derived.validation_namespace])
  client_namespaces = toset([local.e.application.namespace, local.e.derived.validation_namespace])
}

resource "kubernetes_namespace_v1" "environment" {
  for_each = local.namespaces
  metadata {
    name = each.value
    labels = merge(local.e.derived.labels, {
      "pod-security.kubernetes.io/enforce" = "restricted"
    })
  }
  lifecycle { prevent_destroy = true }
}

resource "kubernetes_service_account_v1" "application" {
  metadata {
    name      = local.e.application.service_account
    namespace = local.e.application.namespace
    labels    = local.e.derived.labels
  }
  automount_service_account_token = false
  depends_on                      = [kubernetes_namespace_v1.environment]
}

resource "kubernetes_network_policy_v1" "default_deny" {
  for_each = local.namespaces
  metadata {
    name      = "${local.e.environment_id}-default-deny"
    namespace = each.value
    labels    = local.e.derived.labels
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
  depends_on = [kubernetes_namespace_v1.environment]
}

resource "kubernetes_network_policy_v1" "dns" {
  for_each = local.namespaces
  metadata {
    name      = "${local.e.environment_id}-dns"
    namespace = each.value
    labels    = local.e.derived.labels
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = local.e.network.dns_namespace } }
        pod_selector { match_labels = local.e.network.dns_labels }
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
    }
  }
  depends_on = [kubernetes_namespace_v1.environment]
}

resource "kubernetes_network_policy_v1" "postgres_clients" {
  for_each = local.e.postgres.enabled ? local.client_namespaces : toset([])
  metadata {
    name      = "${local.e.environment_id}-postgres-client"
    namespace = each.value
    labels    = local.e.derived.labels
  }
  spec {
    pod_selector { match_labels = { "local-k8s-platform/access-postgres" = "true" } }
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = local.e.derived.namespace } }
        pod_selector { match_labels = { "app.kubernetes.io/name" = local.e.derived.postgres_name } }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }
  }
  depends_on = [kubernetes_namespace_v1.environment]
}

resource "kubernetes_network_policy_v1" "kafka_clients" {
  for_each = local.e.kafka.enabled ? local.client_namespaces : toset([])
  metadata {
    name      = "${local.e.environment_id}-kafka-client"
    namespace = each.value
    labels    = local.e.derived.labels
  }
  spec {
    pod_selector { match_labels = { "local-k8s-platform/access-kafka" = "true" } }
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = local.e.kafka.namespace } }
        pod_selector { match_labels = { "strimzi.io/cluster" = local.e.derived.kafka_name } }
      }
      ports {
        port     = "9093"
        protocol = "TCP"
      }
    }
  }
  depends_on = [kubernetes_namespace_v1.environment]
}

output "interfaces" {
  description = "Owned namespaces and the non-privileged application identity."
  value = {
    application_namespace = local.e.application.namespace
    data_namespace        = local.e.derived.namespace
    validation_namespace  = local.e.derived.validation_namespace
    service_account       = local.e.application.service_account
  }
}
