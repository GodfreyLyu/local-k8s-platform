# Purpose: Provision one environment-owned Local Path controller and a non-default Retain/WFFC class.
locals {
  name         = "${local.e.environment_id}-local-path"
  namespace    = "${local.e.environment_id}-storage"
  provisioner  = "local-k8s-platform.io/${local.e.environment_id}"
  labels       = merge(local.e.derived.labels, { "app.kubernetes.io/name" = "${local.e.environment_id}-local-path" })
  nodes        = local.e.storage.nodes
  base_path    = "/var/local-k8s-platform/${local.e.environment_id}"
  helper_image = "docker.io/library/busybox:1.38.0@sha256:a46257e06c82ad85242d0d38077d0a52d9896389ca60a1a59d3463db586e5fa4"
}
resource "kubernetes_namespace_v1" "storage" {
  metadata {
    name   = local.namespace
    labels = merge(local.labels, { "pod-security.kubernetes.io/enforce" = "privileged" })
  }
  lifecycle { prevent_destroy = true }
}
resource "kubernetes_service_account_v1" "controller" {
  metadata {
    name      = local.name
    namespace = kubernetes_namespace_v1.storage.metadata[0].name
    labels    = local.labels
  }
  automount_service_account_token = true
}
resource "kubernetes_role_v1" "helper" {
  metadata {
    name      = local.name
    namespace = kubernetes_namespace_v1.storage.metadata[0].name
    labels    = local.labels
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list", "watch", "create", "patch", "update", "delete"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods/log"]
    verbs      = ["get"]
  }
  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = [local.name]
    verbs          = ["get"]
  }
}
resource "kubernetes_cluster_role_v1" "provisioner" {
  metadata {
    name   = local.name
    labels = local.labels
  }
  rule {
    api_groups = [""]
    resources  = ["nodes", "persistentvolumeclaims"]
    verbs      = ["get", "list", "watch"]
  }
  rule {
    api_groups = [""]
    resources  = ["persistentvolumes"]
    verbs      = ["get", "list", "watch", "create", "patch", "update", "delete"]
  }
  rule {
    api_groups = [""]
    resources  = ["events"]
    verbs      = ["create", "patch"]
  }
  rule {
    api_groups = ["storage.k8s.io"]
    resources  = ["storageclasses"]
    verbs      = ["get", "list", "watch"]
  }
}
resource "kubernetes_role_binding_v1" "helper" {
  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.helper.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.controller.metadata[0].name
    namespace = local.namespace
  }
}
resource "kubernetes_cluster_role_binding_v1" "provisioner" {
  metadata {
    name   = local.name
    labels = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.provisioner.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.controller.metadata[0].name
    namespace = local.namespace
  }
}
resource "kubernetes_config_map_v1" "configuration" {
  metadata {
    name      = local.name
    namespace = kubernetes_namespace_v1.storage.metadata[0].name
    labels    = local.labels
  }
  data = {
    "config.json" = jsonencode({ nodePathMap = [for node in local.nodes : { node = node, paths = [local.base_path] }], cmdTimeoutSeconds = 120 })
    # The helper receives only the new provisioner's unique base path. Require
    # requested bytes plus the configured disk reserve; this is a guard, not disk quota enforcement.
    setup = <<-SH
      #!/bin/sh
      # Purpose: Allocate only this environment's volumes after checking disk headroom.
      set -eu
      case "$VOL_DIR" in ${local.base_path}/*) ;; *) exit 1 ;; esac
      available_kib="$(df -Pk ${local.base_path} | awk 'NR==2 {print $4}')"
      required_kib="$((VOL_SIZE_BYTES / 1024 + ${local.e.budget.min_disk_free_mib} * 1024))"
      [ "$available_kib" -ge "$required_kib" ] || { echo 'insufficient local disk headroom' >&2; exit 1; }
      mkdir -m 0777 -p "$VOL_DIR"
    SH
    # Retain is the primary control; no-op also protects files if a PV is later
    # manually marked Delete. Released directories need explicit human cleanup.
    teardown = "#!/bin/sh\n# Purpose: Preserve retained volume contents during controller cleanup.\nset -eu\nexit 0\n"
    "helperPod.yaml" = yamlencode({
      apiVersion = "v1", kind = "Pod", metadata = { name = "${local.e.environment_id}-storage-helper", labels = local.labels }
      spec = {
        automountServiceAccountToken = false
        securityContext              = { seccompProfile = { type = "RuntimeDefault" } }
        containers                   = [{ name = "helper-pod", image = local.helper_image, imagePullPolicy = "IfNotPresent", securityContext = { allowPrivilegeEscalation = false, readOnlyRootFilesystem = true, capabilities = { drop = ["ALL"] } }, resources = { requests = { cpu = "10m", memory = "16Mi" }, limits = { cpu = "200m", memory = "64Mi" } } }]
      }
    })
  }
  lifecycle { prevent_destroy = true }
}
resource "kubernetes_deployment_v1" "controller" {
  metadata {
    name      = local.name
    namespace = kubernetes_namespace_v1.storage.metadata[0].name
    labels    = local.labels
  }
  spec {
    replicas = 1
    strategy { type = "Recreate" } # Never run competing provisioner instances.
    selector { match_labels = { "app.kubernetes.io/name" = local.name } }
    template {
      metadata { labels = local.labels }
      spec {
        service_account_name = kubernetes_service_account_v1.controller.metadata[0].name
        security_context {
          run_as_non_root = true
          run_as_user     = 1000
          run_as_group    = 1000
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name              = "local-path-provisioner"
          image             = "docker.io/rancher/local-path-provisioner:v0.0.37@sha256:e757967a5ec338f6a9b371c5a9688bedaa8c3578ea3dd4db329ea0084be0a86f"
          image_pull_policy = "IfNotPresent"
          command           = ["local-path-provisioner", "start", "--config", "/etc/config/config.json", "--namespace", local.namespace, "--configmap-name", local.name, "--service-account-name", local.name, "--provisioner-name", local.provisioner, "--helper-image", local.helper_image, "--worker-threads", "1"]
          resources {
            requests = { cpu = "50m", memory = "64Mi" }
            limits   = { cpu = "200m", memory = "128Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          env {
            name  = "CONFIG_MOUNT_PATH"
            value = "/etc/config/"
          }
          env {
            name  = "HEALTH_PORT"
            value = "8080"
          }
          port {
            name           = "health"
            container_port = 8080
          }
          readiness_probe {
            http_get {
              path = "/ready"
              port = "health"
            }
            initial_delay_seconds = 5
            period_seconds        = 5
          }
          liveness_probe {
            http_get {
              path = "/health"
              port = "health"
            }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
          volume_mount {
            name       = "config"
            mount_path = "/etc/config"
            read_only  = true
          }
        }
        volume {
          name = "config"
          config_map { name = kubernetes_config_map_v1.configuration.metadata[0].name }
        }
      }
    }
  }
  depends_on = [kubernetes_cluster_role_binding_v1.provisioner, kubernetes_role_binding_v1.helper]
  lifecycle { prevent_destroy = true }
}
resource "kubernetes_storage_class_v1" "local_retain" {
  metadata {
    name        = local.e.storage.class
    labels      = local.labels
    annotations = { "storageclass.kubernetes.io/is-default-class" = "false", defaultVolumeType = "local" }
  }
  storage_provisioner    = local.provisioner
  volume_binding_mode    = "WaitForFirstConsumer"
  reclaim_policy         = "Retain"
  allow_volume_expansion = false
  parameters             = { nodeAffinityKey = "kubernetes.io/hostname" }
  depends_on             = [kubernetes_deployment_v1.controller]
  lifecycle { prevent_destroy = true }
}
output "storage_class" {
  value = kubernetes_storage_class_v1.local_retain.metadata[0].name
}
