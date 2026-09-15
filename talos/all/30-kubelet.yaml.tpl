---
machine:
  kubelet:
    defaultRuntimeSeccompProfileEnabled: true
    extraConfig:
      imageGCHighThresholdPercent: 85
      imageGCLowThresholdPercent: 65
      maxParallelImagePulls: 10
      maxPods: 400
      serializeImagePulls: false
      serverTLSBootstrap: true
      shutdownGracePeriod: 5m
      shutdownGracePeriodCriticalPods: 2m
    nodeIP:
      validSubnets:
        - 10.1.1.0/24
        - {{ .Data.CLUSTER_NODE_V6_CIDR }}
