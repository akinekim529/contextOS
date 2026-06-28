{{/*
Reusable PodDisruptionBudget for any ContextOS service subchart.
Usage:  {{ include "contextos-common.pdb" (dict "name" "memory" "minAvailable" 2 "root" .) }}
*/}}
{{- define "contextos-common.pdb" -}}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ .name }}
spec:
  minAvailable: {{ .minAvailable | default 2 }}
  selector:
    matchLabels:
      app.kubernetes.io/component: {{ .name }}
{{- end -}}

{{/*
Default NetworkPolicy: deny-all ingress except from the gateway. Zero-trust at the network
layer mirrors the zero-trust tenant model at the data layer.
*/}}
{{- define "contextos-common.denyExceptGateway" -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ .name }}-deny-except-gateway
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/component: {{ .name }}
  policyTypes: ["Ingress"]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app.kubernetes.io/component: gateway
{{- end -}}
