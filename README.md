# ⛵ flatops

A GitOps-managed Kubernetes homelab cluster running on [Talos Linux](https://www.talos.dev/).

## 📋 Overview

This repository contains the declarative configuration for **kantai**, a bare-metal Kubernetes cluster. The cluster is designed for home infrastructure workloads with a focus on:

- **GitOps-driven operations** via FluxCD
- **Secure networking** with Cilium in kube-proxy replacement mode
- **Distributed storage** using Rook-Ceph
- **GPU workloads** with NVIDIA GPU Operator
- **Comprehensive observability** using VictoriaMetrics and Grafana
- **Continuous integration** via Renovate

## 🏗️ Cluster Architecture

### Nodes

| Node | Role | Hardware |
|------|------|----------|
| kantai1 | Hyper-converged control plane and workloads | <ul><li>AMD EPYC 7443P, 64 GiB</li><li>NVIDIA RTX 4000 Ada Generation, 24 GB</li><li>Micron 9300 PRO, 4 TB, x7</li><li>Seagate Exos X20, 18 TB, x15</li><li>NVIDIA ConnectX-5</li><li>LSI 9500-8e</li><li>45Drives HL-15</li></ul> |
| kantai2 | Virtual arm64 control plane and workloads | <ul><li>Apple M2 Mac Mini, 16 GB (mem), 500 GB (block)</li><li>UTM + QEMU hypervisor</li></ul> |
| kantai3 | Hyper-converged control plane and workloads | <ul><li>AMD Ryzen Embedded V1500B, 32 GB</li><li>NVIDIA T400, 4 GB</li><li>Seagate Exos X18, 18 TB, x6</li><li>NVIDIA ConnectX-3</li><li>QNAP TS-673A</li></ul> |

### Infrastructure Stack

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              Applications                               │
├─────────────────────────────────────────────────────────────────────────┤
│  Envoy Gateway │ external-dns │ Tailscale │ cert-manager │ Pocket ID    │
├─────────────────────────────────────────────────────────────────────────┤
│  VictoriaMetrics │ Grafana │ fluent-bit │ kube-prometheus-stack         │
├─────────────────────────────────────────────────────────────────────────┤
│  Rook-Ceph │ OpenEBS ZFS │ Samba │ VolSync → Cloudflare R2              │
├─────────────────────────────────────────────────────────────────────────┤
│  CloudNative-PG │ NVIDIA GPU Operator │ Multus CNI                      │
├─────────────────────────────────────────────────────────────────────────┤
│  Cilium (kube-proxy replacement, BGP, Network Policies)                 │
├─────────────────────────────────────────────────────────────────────────┤
│                        Talos Linux + Kubernetes                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Network infrastructure

**kantai** sits on top of an all-[Ubiquiti](https://ui.com/) network, with a [Hi-Capacity Aggregation](https://store.ui.com/us/en/category/switching-aggregation/products/usw-pro-aggregation) as the TOR and a [Dream Machine Pro](https://store.ui.com/us/en/category/all-cloud-gateways/products/udm-pro) as the gateway/router/firewall. Recent versions of Unifi Network and Unifi OS support [BGP](https://help.ui.com/hc/en-us/articles/16271338193559-UniFi-Border-Gateway-Protocol-BGP), which is used to advertise load balancer addresses and thus provide node-balanced cluster services to the network.

## 🔧 Core Components

### GitOps & Cluster Management

#### FluxCD

The cluster is managed entirely through GitOps using [FluxCD](https://fluxcd.io/). All resources are declared in this repository and automatically reconciled to the cluster. The [Flux Operator](https://fluxcd.control-plane.io/operator/) manages the FluxCD instance.

- **Kustomizations** define the desired state of each application
- **HelmReleases** manage Helm chart deployments
- **OCIRepositories** pull charts from OCI registries
- Drift detection ensures cluster state matches Git

#### tuppr

Automated Talos and Kubernetes upgrades are managed by [tuppr](https://github.com/home-operations/tuppr). Upgrade CRDs (`TalosUpgrade`, `KubernetesUpgrade`) define version targets with health checks that ensure VolSync backups complete and Ceph cluster health is OK before proceeding.

#### Renovate

This repository is constantly updated using [Renovate](https://docs.renovatebot.com/) and [flux-local](https://github.com/allenporter/flux-local). Minor and patch updates are applied automatically while major releases require human approval.

### Networking

#### Cilium

[Cilium](https://cilium.io/) serves as the CNI in **kube-proxy replacement mode**, providing:

- **eBPF-based networking** with native routing
- **BGP Control Plane** for advertising service IPs to the network with load-balancing
- **Network Policies** for pod-level traffic control
- **Bandwidth Manager** with BBR congestion control
- **IPv4/IPv6 dual-stack** with BIG TCP support

#### Envoy Gateway

[Envoy Gateway](https://gateway.envoyproxy.io/) implements the Kubernetes Gateway API for HTTP/HTTPS routes and load balancing. It provides the primary entry points for cluster services.

#### external-dns

[external-dns](https://github.com/kubernetes-sigs/external-dns) automatically manages DNS records for services:

- **Cloudflare** for public DNS
- **UniFi** for internal DNS

#### Tailscale

The [Tailscale Operator](https://tailscale.com/kubernetes-operator) provides secure remote access to cluster services via a mesh VPN, including API server proxy functionality.

#### Multus

[Multus CNI](https://github.com/k8snetworkplumbingwg/multus-cni) enables attaching multiple network interfaces to pods. Used for workloads requiring direct LAN access via macvlan interfaces with dual-stack networking support.

### Secrets Management

#### external-secrets + 1Password

[external-secrets](https://external-secrets.io/) synchronizes secrets from 1Password into Kubernetes using the 1Password Connect server. A `ClusterSecretStore` provides cluster-wide access to secrets.

### Certificate Management

#### cert-manager + trust-manager

[cert-manager](https://cert-manager.io/) automates certificate lifecycle management:

- ACME (Let's Encrypt) certificates for public services
- Internal CA for cluster services
- [trust-manager](https://cert-manager.io/docs/trust-manager/) distributes CA bundles across namespaces

### Identity & Authentication

#### Pocket ID

[Pocket ID](https://github.com/pocket-id/pocket-id) serves as the in-cluster OIDC provider, enabling:

- Kubernetes API server OIDC authentication
- OAuth2 authentication for cluster services via Envoy Gateway
- Centralized identity management for applications

### Storage

#### Rook-Ceph

[Rook-Ceph](https://rook.io/) provides distributed storage across the cluster:

- **Block Storage** (`ceph-block`) - Default storage class with 3-way replication, LZ4 compression
- **Object Storage** (`ceph-bucket`) - S3-compatible storage with erasure coding (2+1)
- **Dashboard** exposed via Envoy Gateway
- Encrypted OSDs for data-at-rest security

#### OpenEBS ZFS

[OpenEBS ZFS LocalPV](https://openebs.io/) exposes existing ZFS pools on nodes as Kubernetes storage:

- Provides access to large media and data pools
- Supports ZFS features (compression, snapshots, datasets)
- Used for workloads requiring high-capacity local storage

#### Samba

Samba deployments on storage nodes share ZFS-backed volumes to the local network via SMB, enabling access to cluster-managed data from non-Kubernetes clients.

#### VolSync + Kopia

[VolSync](https://volsync.readthedocs.io/) backs up persistent volumes to **Cloudflare R2** using Kopia:

- Daily snapshots with 7 daily, 4 weekly, 12 monthly retention
- Clone-based backups (no application downtime)
- Zstd compression for efficient storage

### Database

#### CloudNative-PG

[CloudNative-PG](https://cloudnative-pg.io/) manages PostgreSQL clusters for applications:

- PostgreSQL 18 with **vchord** vector extensions for AI/ML workloads
- WAL archiving via barman-cloud plugin
- Automated backups and point-in-time recovery

### GPU Compute

#### NVIDIA GPU Operator

The [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/) enables GPU workloads:

- Automatic container toolkit management
- CDI (Container Device Interface) support
- Time-slicing for GPU sharing
- DCGM metrics for monitoring

### Observability

#### Metrics: VictoriaMetrics

The [VictoriaMetrics Operator](https://docs.victoriametrics.com/operator/) manages the metrics stack:

- **VMSingle** for metrics storage (12-week retention on Ceph block storage)
- **VMAgent** for metric collection
- **VMAlert** + **VMAlertmanager** for alerting
- OpenTelemetry integration with Prometheus naming

#### Dashboards: Grafana Operator

The [Grafana Operator](https://grafana.github.io/grafana-operator/) manages Grafana instances and dashboards:

- Declarative dashboard management via `GrafanaDashboard` CRDs
- Automated datasource configuration
- Integrated with VictoriaMetrics

#### Logs: fluent-bit

[fluent-bit](https://fluentbit.io/) collects container logs from all nodes, running as a DaemonSet in the observability-agents namespace.

#### kube-prometheus-stack

The [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) provides:

- ServiceMonitors for Kubernetes components (API server, kubelet, etcd, scheduler, controller-manager)
- kube-state-metrics for resource metrics
- Dashboards via Grafana Operator integration

> **Note:** Prometheus and Alertmanager from this stack are disabled in favor of VictoriaMetrics. The stack is primarily used for its comprehensive ServiceMonitor definitions and dashboards.

## 📁 Repository Structure

```
├── kubernetes/                  # Kubernetes resources
│   ├── apps/                    # Deployments by namespace
│   │   ├── cert-manager/
│   │   ├── cnpg-system/
│   │   ├── database/            # Databases (postgres, influxdb)
│   │   ├── default/             # Most applications
│   │   ├── external-secrets/
│   │   ├── flux-system/
│   │   ├── gpu-operator/        # NVIDIA GPU operator
│   │   ├── kube-system/         # Core infrastructure (Cilium, CoreDNS, etc.)
│   │   ├── network/             # Networking (Envoy Gateway, external-dns, etc.)
│   │   ├── observability/       # Observability stack
│   │   ├── observability-agents/# Privileged observability agents
│   │   ├── openebs-system/
│   │   ├── rook-ceph/
│   │   ├── storage/             # Samba
│   │   ├── tailscale/
│   │   ├── talos-admin/         # Talos management (backups, tuppr)
│   │   └── volsync-system/
│   ├── components/              # Reusable Kustomize components
│   └── transformers/            # Global Kustomize transformers
├── talos/                       # Talos configuration
└── Taskfile.yaml                # Task runner commands
```

## 🚀 Getting Started

### Bootstrap

Bootstrap is currently broken and unusable. I love my pets.

### Maintenance

**Update Talos node configuration:**

```sh
task talos:gen-mc
task talos:apply-mc
```

## 🔒 Security

- **Talos Linux** provides an immutable, minimal OS with no SSH access
- **Secure Boot** enabled on supported nodes with TPM-backed disk encryption
- **Pod Security Standards** enforced via ValidatingAdmissionPolicies
- **Network Policies** via Cilium restrict pod-to-pod traffic
- **OIDC authentication** for Kubernetes API via Pocket ID

## 📊 Monitoring

Lots of dashboards available on the on-cluster Grafana instance. Alerts go out to Discord.

## 🙏 Acknowledgments

- This cluster originally started from [onedr0p/cluster-template](https://github.com/onedr0p/cluster-template), which is absolutely amazing. It makes running Kubernetes at home easy.
- The [Home Operations](https://discord.gg/home-operations) community is amazing as well and will help you. Please join us.
- [Sidero Labs](https://www.siderolabs.com/) for creating an amazing Kubernetes-native system.
- All the Kubernetes SIG groups for maintaining and evolving the world's open, extensible, at-scale resources and workloads orchestration system.
