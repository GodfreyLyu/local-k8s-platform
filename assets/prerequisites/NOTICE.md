<!-- Purpose: Attribute vendored operator resources and describe the maintained installation profile. -->

# Operator manifest sources

`strimzi.json` derives from the official Strimzi 1.3.0 release bundle.
`monitoring.json` derives from the Prometheus Operator v0.95.1 bundle.
Each file records its source URL and the SHA-256 of the downloaded YAML.
The packaged JSON checksums are in `assets/infra/versions.json`.

The upstream CRD schemas and RBAC rules are preserved. Namespace fields and
ServiceAccount subjects use a placeholder. Operator images use fixed digests.
The Prometheus Operator watches the configured monitoring namespace. Kubelet
Service management and watches for referenced objects in other namespaces are
disabled. Its config reloader requests 16Mi of memory.

`prometheus.json` is this project's minimal Prometheus profile. It uses temporary
storage and selects monitors and rules by the configured release label.

The upstream projects license these resources under Apache License 2.0.
See [the license](LICENSE.md),
[Strimzi](https://github.com/strimzi/strimzi-kafka-operator/tree/1.3.0), and
[Prometheus Operator](https://github.com/prometheus-operator/prometheus-operator/tree/v0.95.1).
