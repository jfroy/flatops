---
cluster:
  network:
    cni:
      name: none  # No Flannel, cluster uses Cilium
    podSubnets:
      - 10.11.0.0/16
      - {{ .Data.CLUSTER_POD_V6_CIDR }}
    serviceSubnets:
      - 10.12.0.0/16
      - {{ .Data.CLUSTER_SVC_V6_CIDR }}
