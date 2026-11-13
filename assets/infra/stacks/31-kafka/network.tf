# Purpose: Isolate only this Kafka instance while allowing internal control, DNS, API, and selected clients.
resource "kubernetes_network_policy_v1" "deny" {
  metadata {
    name      = "${local.name}-default-deny"
    namespace = local.namespace
    labels    = local.labels
  }
  spec {
    pod_selector { match_labels = { "strimzi.io/cluster" = local.name } }
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_network_policy_v1" "control" {
  metadata {
    name      = "${local.name}-control"
    namespace = local.namespace
    labels    = local.labels
  }
  spec {
    pod_selector { match_labels = { "strimzi.io/cluster" = local.name } }
    policy_types = ["Ingress", "Egress"]
    ingress {
      from {
        pod_selector { match_labels = { "strimzi.io/cluster" = local.name } }
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
          port     = "9404"
          protocol = "TCP"
        }
      }
    }
    dynamic "ingress" {
      for_each = [local.e.application.namespace, local.e.derived.validation_namespace]
      content {
        from {
          namespace_selector { match_labels = { "kubernetes.io/metadata.name" = ingress.value } }
          pod_selector { match_labels = { "local-k8s-platform/access-kafka" = "true" } }
        }
        ports {
          port     = "9093"
          protocol = "TCP"
        }
      }
    }
    egress {
      to {
        pod_selector { match_labels = { "strimzi.io/cluster" = local.name } }
      }
    }
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
    dynamic "egress" {
      for_each = toset(local.e.network.api_server_cidrs)
      content {
        to {
          ip_block { cidr = egress.value }
        }
        ports {
          port     = "443"
          protocol = "TCP"
        }
        ports {
          port     = "8443"
          protocol = "TCP"
        }
      }
    }
  }
}
