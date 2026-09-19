---
apiVersion: v1alpha1
kind: KubeletConfig
defaultRuntimeSeccompProfileEnabled: true
config:
  imageGCHighThresholdPercent: 85
  imageGCLowThresholdPercent: 65
  maxParallelImagePulls: 10
  maxPods: 400
  serializeImagePulls: false
  serverTLSBootstrap: true
  shutdownGracePeriod: 5m
  shutdownGracePeriodCriticalPods: 2m
---
apiVersion: v1alpha1
kind: KubeNodeConfig
nodeIP:
  validSubnets:
    - 10.1.1.0/24
    - {{ .Data.CLUSTER_NODE_V6_CIDR }}
