# Purpose: Run one persistent TLS PostgreSQL instance using externally prepared Secret references.
locals {
  namespace = local.e.derived.namespace
  name      = local.e.derived.postgres_name
  selector = {
    "app.kubernetes.io/name"      = local.e.derived.postgres_name
    "local-k8s-platform/database" = local.e.derived.postgres_name
  }
  labels = merge(local.e.derived.labels, local.selector, {
    "app.kubernetes.io/component" = "postgresql"
  })
  postgres_image = "docker.io/library/postgres:17.11@sha256:ff87138949ad9a46f32ff23bd6852b52ce024a13dffe9b4087be98292cdd3b4f"
  # Pin inherited from the platform-infra version record; this package has not run the image.
  # https://github.com/prometheus-community/postgres_exporter/releases/tag/v0.20.1
  exporter_image = "quay.io/prometheuscommunity/postgres-exporter:v0.20.1@sha256:ac5ec343104fae0e2d84a27bb8d69b38430a11910c5382cad85d478d2bab713e"
  configuration = merge({
    for name in ["start.sh", "bootstrap.sql", "postgresql.conf", "check-admin.sql", "check-app.sql", "check-metrics.sql"] : name => file("${path.module}/config/${name}")
    }, {
    "pg_hba.conf" = templatefile("${path.module}/config/pg_hba.conf.tftpl", {
      database = local.e.application.database
      username = local.e.application.username
    })
  })
}

resource "kubernetes_service_account_v1" "postgres" {
  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.labels
  }
  automount_service_account_token = false
}

resource "kubernetes_config_map_v1" "configuration" {
  metadata {
    name      = "${local.name}-config"
    namespace = local.namespace
    labels    = local.labels
  }
  data = local.configuration
}

# Separate from StatefulSet volumeClaimTemplates so ownership and deletion
# protection remain explicit. WFFC binding happens only after Pod scheduling.
resource "kubernetes_persistent_volume_claim_v1" "data" {
  metadata {
    name      = "${local.name}-data"
    namespace = local.namespace
    labels    = local.labels
  }
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = local.e.storage.class
    volume_mode        = "Filesystem"
    resources { requests = { storage = local.e.postgres.storage } }
  }
  wait_until_bound = false
  lifecycle { prevent_destroy = true }
}

resource "kubernetes_service_v1" "headless" {
  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.labels
  }
  spec {
    cluster_ip = "None"
    selector   = local.selector
    port {
      name        = "postgres"
      port        = 5432
      target_port = "postgres"
    }
  }
}

resource "kubernetes_service_v1" "read_write" {
  metadata {
    name      = "${local.name}-rw"
    namespace = local.namespace
    labels    = local.labels
  }
  spec {
    type     = "ClusterIP"
    selector = local.selector
    port {
      name        = "postgres"
      port        = 5432
      target_port = "postgres"
    }
  }
}

resource "kubernetes_stateful_set_v1" "postgres" {
  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.labels
  }
  spec {
    replicas               = 1
    service_name           = kubernetes_service_v1.headless.metadata[0].name
    pod_management_policy  = "OrderedReady"
    revision_history_limit = 3
    selector { match_labels = local.selector }
    update_strategy { type = "RollingUpdate" }
    template {
      metadata {
        labels = local.labels
        annotations = {
          "local-k8s-platform/config-sha256" = sha256(jsonencode(local.configuration))
        }
      }
      spec {
        service_account_name             = kubernetes_service_account_v1.postgres.metadata[0].name
        automount_service_account_token  = false
        enable_service_links             = false
        termination_grace_period_seconds = 60
        security_context {
          run_as_non_root        = true
          run_as_user            = 999
          run_as_group           = 999
          fs_group               = 999
          fs_group_change_policy = "OnRootMismatch"
          seccomp_profile { type = "RuntimeDefault" }
        }

        container {
          name              = "postgres"
          image             = local.postgres_image
          image_pull_policy = "IfNotPresent"
          command           = ["/bin/bash", "/config/start.sh"]
          env {
            name  = "APP_USERNAME"
            value = local.e.application.username
          }
          env {
            name  = "APP_DATABASE"
            value = local.e.application.database
          }
          env {
            name  = "PGDATA"
            value = "/var/lib/postgresql/data/pgdata"
          }
          port {
            name           = "postgres"
            container_port = 5432
          }
          resources {
            requests = local.e.postgres.resources.requests
            limits   = local.e.postgres.resources.limits
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          startup_probe {
            exec { command = ["/bin/bash", "-c", "test -f /var/run/postgresql/platform-startup-validated && pg_isready -h 127.0.0.1 -p 5432 -U \"$APP_USERNAME\" -d \"$APP_DATABASE\""] }
            failure_threshold = 60
            period_seconds    = 5
            timeout_seconds   = 3
          }
          readiness_probe {
            exec { command = ["/bin/bash", "-c", "test -f /var/run/postgresql/platform-startup-validated && pg_isready -h 127.0.0.1 -p 5432 -U \"$APP_USERNAME\" -d \"$APP_DATABASE\""] }
            period_seconds    = 10
            timeout_seconds   = 3
            failure_threshold = 3
          }
          liveness_probe {
            exec { command = ["/bin/bash", "-c", "test -f /var/run/postgresql/platform-startup-validated && pg_isready -h 127.0.0.1 -p 5432 -U \"$APP_USERNAME\" -d \"$APP_DATABASE\""] }
            period_seconds    = 15
            timeout_seconds   = 3
            failure_threshold = 6
          }
          lifecycle {
            pre_stop {
              exec { command = ["/bin/bash", "-c", "pg_ctl -D \"$PGDATA\" -m fast -w -t 45 stop >/dev/null 2>&1 || true"] }
            }
          }
          volume_mount {
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
          }
          volume_mount {
            name       = "configuration"
            mount_path = "/config"
            read_only  = true
          }
          volume_mount {
            name       = "admin"
            mount_path = "/secrets/admin"
            read_only  = true
          }
          volume_mount {
            name       = "application"
            mount_path = "/secrets/app"
            read_only  = true
          }
          volume_mount {
            name       = "metrics-identity"
            mount_path = "/secrets/metrics"
            read_only  = true
          }
          volume_mount {
            name       = "tls-source"
            mount_path = "/tls-source"
            read_only  = true
          }
          volume_mount {
            name       = "tls-runtime"
            mount_path = "/run/tls"
          }
          volume_mount {
            name       = "runtime"
            mount_path = "/var/run/postgresql"
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
          volume_mount {
            name       = "shared-memory"
            mount_path = "/dev/shm"
          }
        }

        # This identity has pg_monitor, not application table access or superuser.
        # v0.20.1 supports file credentials; no password is a CLI or env value.
        dynamic "container" {
          for_each = local.e.monitoring.enabled ? [1] : []
          content {
            name              = "metrics"
            image             = local.exporter_image
            image_pull_policy = "IfNotPresent"
            args              = ["--web.listen-address=:9187"]
            env {
              name  = "DATA_SOURCE_URI"
              value = "localhost:5432/${local.e.application.database}?sslmode=verify-full&sslrootcert=/run/postgres-ca/ca.crt&connect_timeout=5"
            }
            env {
              name  = "DATA_SOURCE_USER_FILE"
              value = "/run/metrics/username"
            }
            env {
              name  = "DATA_SOURCE_PASS_FILE"
              value = "/run/metrics/password"
            }
            port {
              name           = "metrics"
              container_port = 9187
            }
            resources {
              requests = { cpu = "50m", memory = "64Mi" }
              limits   = { cpu = "250m", memory = "128Mi" }
            }
            security_context {
              run_as_user                = 65534
              run_as_group               = 65534
              run_as_non_root            = true
              allow_privilege_escalation = false
              read_only_root_filesystem  = true
              capabilities { drop = ["ALL"] }
            }
            readiness_probe {
              tcp_socket { port = "metrics" }
              period_seconds = 10
            }
            liveness_probe {
              tcp_socket { port = "metrics" }
              period_seconds = 15
            }
            volume_mount {
              name       = "metrics-identity"
              mount_path = "/run/metrics"
              read_only  = true
            }
            volume_mount {
              name       = "public-ca"
              mount_path = "/run/postgres-ca"
              read_only  = true
            }
            volume_mount {
              name       = "tmp"
              mount_path = "/tmp"
            }
          }
        }

        volume {
          name = "data"
          persistent_volume_claim { claim_name = kubernetes_persistent_volume_claim_v1.data.metadata[0].name }
        }
        volume {
          name = "configuration"
          config_map {
            name         = kubernetes_config_map_v1.configuration.metadata[0].name
            default_mode = "0444"
          }
        }
        volume {
          name = "admin"
          secret {
            secret_name  = "${local.name}-admin"
            default_mode = "0440"
          }
        }
        volume {
          name = "application"
          secret {
            secret_name  = "${local.name}-app"
            default_mode = "0440"
          }
        }
        volume {
          name = "metrics-identity"
          secret {
            secret_name  = "${local.name}-metrics"
            default_mode = "0440"
          }
        }
        volume {
          name = "tls-source"
          secret {
            secret_name  = "${local.name}-tls"
            default_mode = "0440"
          }
        }
        volume {
          name = "public-ca"
          secret {
            secret_name  = "${local.name}-tls"
            default_mode = "0440"
            items {
              key  = "ca.crt"
              path = "ca.crt"
            }
          }
        }
        volume {
          name = "tls-runtime"
          empty_dir {
            medium     = "Memory"
            size_limit = "8Mi"
          }
        }
        volume {
          name = "runtime"
          empty_dir { size_limit = "16Mi" }
        }
        volume {
          name = "tmp"
          empty_dir { size_limit = "64Mi" }
        }
        volume {
          name = "shared-memory"
          empty_dir {
            medium     = "Memory"
            size_limit = "64Mi"
          }
        }
      }
    }
  }
  lifecycle { prevent_destroy = true }
}

output "interfaces" {
  description = "Database endpoints and credential references without credential values."
  value = {
    postgres           = "${local.name}-rw.${local.namespace}.svc.cluster.local:5432"
    database           = local.e.application.database
    application_user   = local.e.application.username
    application_secret = "${local.e.application.namespace}/${local.name}-client"
    server_tls_secret  = "${local.namespace}/${local.name}-tls"
  }
}
