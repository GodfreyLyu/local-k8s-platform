# Purpose: Create a single-node TLS Kafka instance, exact topics, and least-privilege client identities.
locals {
  name      = local.e.derived.kafka_name
  namespace = local.e.kafka.namespace
  labels    = merge(local.e.derived.labels, { "strimzi.io/cluster" = local.name })
  client_peers = [for namespace in [local.e.application.namespace, local.e.derived.validation_namespace] : {
    namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = namespace } }
    podSelector       = { matchLabels = { "local-k8s-platform/access-kafka" = "true" } }
  }]
  users = {
    producer = [for topic in local.e.kafka.topics : {
      resource   = { type = "topic", name = topic, patternType = "literal" }
      operations = ["Write", "Describe"]
      host       = "*"
    }]
    consumer = concat([for topic in local.e.kafka.topics : {
      resource   = { type = "topic", name = topic, patternType = "literal" }
      operations = ["Read", "Describe"]
      host       = "*"
      }], [{
      resource   = { type = "group", name = local.e.kafka.consumer_group, patternType = "literal" }
      operations = ["Read"]
      host       = "*"
    }])
  }
}

resource "kubernetes_config_map_v1" "metrics" {
  count = local.e.monitoring.enabled ? 1 : 0
  metadata {
    name      = "${local.name}-metrics"
    namespace = local.namespace
    labels    = local.labels
  }
  data = {
    "metrics.yml" = yamlencode({
      lowercaseOutputName = true
      rules = [
        { pattern = "kafka.server<type=ReplicaManager, name=UnderReplicatedPartitions><>Value", name = "kafka_under_replicated_partitions", type = "GAUGE" },
        { pattern = "kafka.controller<type=KafkaController, name=ActiveControllerCount><>Value", name = "kafka_active_controller_count", type = "GAUGE" },
        { pattern = "kafka.server<type=BrokerTopicMetrics, name=MessagesInPerSec><>Count", name = "kafka_messages_in_total", type = "COUNTER" }
      ]
    })
  }
}

resource "kubernetes_manifest" "pool" {
  manifest = {
    apiVersion = "kafka.strimzi.io/v1"
    kind       = "KafkaNodePool"
    metadata   = { name = "${local.name}-combined", namespace = local.namespace, labels = local.labels }
    spec = {
      replicas = 1
      roles    = ["controller", "broker"]
      storage = {
        type = "jbod"
        volumes = [{ id = 0, type = "persistent-claim", size = local.e.kafka.storage,
        class = local.e.storage.class, deleteClaim = false, kraftMetadata = "shared" }]
      }
      resources  = local.e.kafka.resources
      jvmOptions = { "-Xms" = "384m", "-Xmx" = "384m" }
    }
  }
  lifecycle { prevent_destroy = true }
}

resource "kubernetes_manifest" "kafka" {
  manifest = {
    apiVersion = "kafka.strimzi.io/v1"
    kind       = "Kafka"
    metadata   = { name = local.name, namespace = local.namespace, labels = local.labels }
    spec = {
      kafka = merge({
        version = "4.3.1"
        image   = "quay.io/strimzi/kafka:1.3.0-kafka-4.3.1@sha256:1f189b1f0f32684302d8b018b1d21b411e2469a9c465fbdc18fa3599cb5bbdad"
        listeners = [{
          name               = "tls"
          port               = 9093
          type               = "internal"
          tls                = true
          authentication     = { type = "tls" }
          configuration      = { useServiceDnsDomain = true }
          networkPolicyPeers = local.client_peers
        }]
        authorization = { type = "simple" }
        config = {
          "default.replication.factor"               = 1
          "min.insync.replicas"                      = 1
          "offsets.topic.replication.factor"         = 1
          "transaction.state.log.replication.factor" = 1
          "transaction.state.log.min.isr"            = 1
          "auto.create.topics.enable"                = false
          "log.retention.hours"                      = 24
          "log.retention.bytes"                      = 268435456
          "log.segment.bytes"                        = 67108864
        }
        }, local.e.monitoring.enabled ? {
        metricsConfig = {
          type      = "jmxPrometheusExporter"
          valueFrom = { configMapKeyRef = { name = kubernetes_config_map_v1.metrics[0].metadata[0].name, key = "metrics.yml" } }
        }
      } : {})
      entityOperator = {
        topicOperator = { resources = { requests = { cpu = "50m", memory = "128Mi" }, limits = { cpu = "300m", memory = "256Mi" } } }
        userOperator  = { resources = { requests = { cpu = "50m", memory = "128Mi" }, limits = { cpu = "300m", memory = "256Mi" } } }
      }
    }
  }
  depends_on = [kubernetes_manifest.pool]
  lifecycle { prevent_destroy = true }
}

resource "kubernetes_manifest" "topic" {
  for_each = toset(local.e.kafka.topics)
  manifest = {
    apiVersion = "kafka.strimzi.io/v1"
    kind       = "KafkaTopic"
    metadata = {
      name      = "${local.e.environment_id}-${substr(sha256(each.value), 0, 12)}"
      namespace = local.namespace
      labels    = local.labels
    }
    spec = {
      topicName  = each.value
      partitions = 3
      replicas   = 1
      config = {
        "min.insync.replicas" = "1"
        "retention.ms"        = "86400000"
        "retention.bytes"     = "268435456"
        "cleanup.policy"      = "delete"
      }
    }
  }
  depends_on = [kubernetes_manifest.kafka]
  lifecycle { prevent_destroy = true }
}

resource "kubernetes_manifest" "user" {
  for_each = local.users
  manifest = {
    apiVersion = "kafka.strimzi.io/v1"
    kind       = "KafkaUser"
    metadata   = { name = "${local.name}-${each.key}", namespace = local.namespace, labels = local.labels }
    spec = {
      authentication = { type = "tls" }
      authorization  = { type = "simple", acls = each.value }
    }
  }
  depends_on = [kubernetes_manifest.kafka]
  lifecycle { prevent_destroy = true }
}

output "interfaces" {
  description = "Kafka addresses, topic names, and application Secret references."
  value = {
    bootstrap       = "${local.name}-kafka-bootstrap.${local.namespace}.svc.cluster.local:9093"
    topics          = local.e.kafka.topics
    consumer_group  = local.e.kafka.consumer_group
    producer_secret = "${local.e.application.namespace}/${local.name}-producer-client"
    consumer_secret = "${local.e.application.namespace}/${local.name}-consumer-client"
  }
}
