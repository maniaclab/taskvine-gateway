{{- define "taskvine-gateway.fullname" -}}
{{- .Release.Name -}}
{{- end -}}

{{- define "taskvine-gateway.labels" -}}
app.kubernetes.io/name: taskvine-gateway
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "taskvine-gateway.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- .Values.serviceAccount.name | default (include "taskvine-gateway.fullname" .) -}}
{{- else -}}
{{- .Values.serviceAccount.name | default "default" -}}
{{- end -}}
{{- end -}}

{{- define "taskvine-gateway.namespace" -}}
{{- .Values.namespace | default .Release.Namespace -}}
{{- end -}}
