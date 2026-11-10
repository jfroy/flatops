---
apiVersion: v1alpha1
kind: KubeletConfig
defaultRuntimeSeccompProfileEnabled: true
config:
  imageGCHighThresholdPercent: 65
  imageGCLowThresholdPercent: 50
  imageMaximumGCAge: 168h
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
