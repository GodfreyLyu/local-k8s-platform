# Purpose: Run the generic device plugin that advertises the krunkit render node as a schedulable GPU resource.
locals {
  # The resource name is "<domain>/<device name>"; keep both halves aligned with derived.gpu_resource.
  gpu_domain = split("/", local.e.derived.gpu_resource)[0]
  gpu_device = split("/", local.e.derived.gpu_resource)[1]
  selector = {
    "app.kubernetes.io/name"     = local.e.derived.gpu_plugin_name
    "app.kubernetes.io/instance" = local.e.environment_id
  }
}

resource "kubernetes_namespace_v1" "gpu" {
  metadata {
    name = local.e.derived.gpu_namespace
    # Device discovery and the kubelet plugin socket need host access, so only this
    # dedicated namespace is privileged. Workloads that request the resource are not.
    labels = merge(local.e.derived.labels, {
      "pod-security.kubernetes.io/enforce" = "privileged"
    })
  }
  lifecycle { prevent_destroy = true }
}

resource "kubernetes_network_policy_v1" "gpu" {
  metadata {
    name      = "${local.e.environment_id}-gpu-plugin"
    namespace = kubernetes_namespace_v1.gpu.metadata[0].name
    labels    = local.e.derived.labels
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
    # The plugin talks to kubelet over a Unix socket and needs no egress. Only the
    # health port stays reachable so kubelet readiness probes work under Calico.
    ingress {
      ports {
        port     = "8080"
        protocol = "TCP"
      }
    }
  }
}

resource "kubernetes_daemon_set_v1" "plugin" {
  metadata {
    name      = local.e.derived.gpu_plugin_name
    namespace = kubernetes_namespace_v1.gpu.metadata[0].name
    labels    = merge(local.e.derived.labels, local.selector)
  }

  spec {
    selector {
      match_labels = local.selector
    }

    template {
      metadata {
        labels = merge(local.e.derived.labels, local.selector)
      }

      spec {
        automount_service_account_token = false
        node_selector                   = { "kubernetes.io/os" = "linux" }

        # Nodes without the render device would fail the hostPath mount, so only
        # the reviewed nodes run the plugin.
        affinity {
          node_affinity {
            required_during_scheduling_ignored_during_execution {
              node_selector_term {
                match_expressions {
                  key      = "kubernetes.io/hostname"
                  operator = "In"
                  values   = local.e.gpu.nodes
                }
              }
            }
          }
        }

        container {
          name              = "generic-device-plugin"
          image             = "docker.io/squat/generic-device-plugin:0.2.0@sha256:66c8d5c270eb2b721f1064c549b9b7898152a6d2f0163380a5d37dc7636c20ff"
          image_pull_policy = "IfNotPresent"
          args = [
            "--domain=${local.gpu_domain}",
            "--device",
            jsonencode({
              name = local.gpu_device
              groups = [{
                # Slots above 1 share one device without GPU memory isolation.
                count = local.e.gpu.slots
                paths = [{ path = local.e.derived.gpu_device }]
              }]
            })
          ]

          security_context {
            privileged = true
          }

          resources {
            requests = { cpu = "50m", memory = "32Mi" }
            limits   = { cpu = "200m", memory = "128Mi" }
          }

          port {
            name           = "health"
            container_port = 8080
          }
          readiness_probe {
            http_get {
              path = "/health"
              port = "health"
            }
          }

          volume_mount {
            name       = "device-plugins"
            mount_path = "/var/lib/kubelet/device-plugins"
          }
          volume_mount {
            name       = "dri"
            mount_path = "/dev/dri"
            read_only  = true
          }
        }

        volume {
          name = "device-plugins"
          host_path {
            path = "/var/lib/kubelet/device-plugins"
            type = "Directory"
          }
        }
        volume {
          name = "dri"
          host_path {
            path = "/dev/dri"
            type = "Directory"
          }
        }
      }
    }
  }

  wait_for_rollout = true
  timeouts {
    create = "5m"
    update = "5m"
    delete = "5m"
  }

  depends_on = [kubernetes_network_policy_v1.gpu]
}

output "interfaces" {
  description = "Resource name and slots that workloads request; not a measurement of free GPU capacity."
  value = {
    namespace     = kubernetes_namespace_v1.gpu.metadata[0].name
    resource_name = local.e.derived.gpu_resource
    nodes         = local.e.gpu.nodes
    slots         = local.e.gpu.slots
  }
}
