# Purpose: Create a retained worker-scoped StorageClass using the existing Minikube CSI driver.
resource "kubernetes_storage_class_v1" "local" {
  metadata {
    name   = local.e.storage.class
    labels = local.e.derived.labels
  }
  storage_provisioner = "hostpath.csi.k8s.io"
  volume_binding_mode = "WaitForFirstConsumer"
  reclaim_policy      = "Retain"
  allowed_topologies {
    match_label_expressions {
      key    = "topology.hostpath.csi/node"
      values = local.e.storage.nodes
    }
  }
  lifecycle { prevent_destroy = true }
}
