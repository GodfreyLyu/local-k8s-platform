# Purpose: Connect enabled data services to the existing Prometheus and define scoped availability alerts.
locals {
  labels        = merge(local.e.derived.labels, { release = local.e.monitoring.release })
  postgres_pods = "namespace=\"${local.e.derived.namespace}\",pod=~\"${local.e.derived.postgres_name}-[0-9]+\""
  kafka_pods    = "namespace=\"${local.e.kafka.namespace}\",pod=~\"${local.e.derived.kafka_name}-.+\""
  alert_labels  = { severity = "warning", environment = local.e.environment_id }
  postgres_rules = local.e.postgres.enabled ? [
    {
      alert       = "LocalPlatformPostgresUnavailable"
      expr        = "(pg_up{${local.postgres_pods}} == 0) or absent(pg_up{${local.postgres_pods}}) or (up{${local.postgres_pods}} == 0)"
      for         = "3m"
      labels      = local.alert_labels
      annotations = { summary = "PostgreSQL connection or scrape is unavailable" }
    },
    {
      alert       = "LocalPlatformPostgresCollectorIncomplete"
      expr        = "(pg_exporter_last_scrape_error{${local.postgres_pods}} == 1) or absent(pg_stat_database_numbackends{${local.postgres_pods},datname=\"${local.e.application.database}\"})"
      for         = "10m"
      labels      = local.alert_labels
      annotations = { summary = "Inspect PostgreSQL exporter credentials, TLS, and collector status" }
    }
  ] : []
  kafka_rules = local.e.kafka.enabled ? [
    {
      alert       = "LocalPlatformKafkaUnavailable"
      expr        = "absent(kafka_under_replicated_partitions{${local.kafka_pods}}) or (up{${local.kafka_pods}} == 0) or (kafka_under_replicated_partitions{${local.kafka_pods}} > 0)"
      for         = "5m"
      labels      = local.alert_labels
      annotations = { summary = "Kafka metrics are missing or partitions are under replicated" }
    }
  ] : []
}

resource "kubernetes_manifest" "postgres_monitor" {
  count = local.e.postgres.enabled ? 1 : 0
  manifest = {
    apiVersion = "monitoring.coreos.com/v1"
    kind       = "PodMonitor"
    metadata   = { name = "${local.e.environment_id}-postgres", namespace = local.e.monitoring.namespace, labels = local.labels }
    spec = {
      namespaceSelector   = { matchNames = [local.e.derived.namespace] }
      selector            = { matchLabels = { "app.kubernetes.io/name" = local.e.derived.postgres_name } }
      podMetricsEndpoints = [{ port = "metrics", interval = "60s", scrapeTimeout = "10s" }]
    }
  }
}

resource "kubernetes_manifest" "kafka_monitor" {
  count = local.e.kafka.enabled ? 1 : 0
  manifest = {
    apiVersion = "monitoring.coreos.com/v1"
    kind       = "PodMonitor"
    metadata   = { name = "${local.e.environment_id}-kafka", namespace = local.e.monitoring.namespace, labels = local.labels }
    spec = {
      namespaceSelector   = { matchNames = [local.e.kafka.namespace] }
      selector            = { matchLabels = { "strimzi.io/cluster" = local.e.derived.kafka_name, "strimzi.io/kind" = "Kafka" } }
      podMetricsEndpoints = [{ port = "tcp-prometheus", interval = "60s", scrapeTimeout = "10s" }]
    }
  }
}

resource "kubernetes_manifest" "rules" {
  manifest = {
    apiVersion = "monitoring.coreos.com/v1"
    kind       = "PrometheusRule"
    metadata   = { name = "${local.e.environment_id}-data", namespace = local.e.monitoring.namespace, labels = local.labels }
    spec       = { groups = [{ name = "${local.e.environment_id}.data", interval = "60s", rules = concat(local.postgres_rules, local.kafka_rules) }] }
  }
}
