---
apiVersion: v1alpha1
kind: KubeNetworkConfig
podSubnets:
  - 10.11.0.0/16
  - {{ .Data.CLUSTER_POD_V6_CIDR }}
serviceSubnets:
  - 10.12.0.0/16
  - {{ .Data.CLUSTER_SVC_V6_CIDR }}
nodeCIDRMaskSizeIPv6: 108
---
# No Flannel, cluster uses Cilium. Omitting the document is not enough — the
# generator emits KubeFlannelCNIConfig by default, so it has to be deleted.
apiVersion: v1alpha1
kind: KubeFlannelCNIConfig
$patch: delete
