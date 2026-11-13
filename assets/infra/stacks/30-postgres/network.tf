# Purpose: Allow only labeled clients and the reused Prometheus to reach PostgreSQL.
resource "kubernetes_network_policy_v1" "postgres" {
  metadata {
    name      = "${local.name}-ingress"
    namespace = local.namespace
    labels    = local.labels
  }
  spec {
    pod_selector { match_labels = local.selector }
    policy_types = ["Ingress"]
    ingress {
      dynamic "from" {
        for_each = [local.e.application.namespace, local.e.derived.validation_namespace]
        content {
          namespace_selector { match_labels = { "kubernetes.io/metadata.name" = from.value } }
          pod_selector { match_labels = { "local-k8s-platform/access-postgres" = "true" } }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }
    dynamic "ingress" {
      for_each = local.e.monitoring.enabled ? [1] : []
      content {
        from {
          namespace_selector { match_labels = { "kubernetes.io/metadata.name" = local.e.monitoring.namespace } }
          pod_selector { match_labels = local.e.monitoring.pod_labels }
        }
        ports {
          port     = "9187"
          protocol = "TCP"
        }
      }
    }
  }
}
