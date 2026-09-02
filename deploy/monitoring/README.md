# TVT monitoring deployment

This directory contains the single-node profile from `MONITORING.md`. It
installs one Prometheus, Alertmanager, Grafana, Loki, and Alloy instance; keeps
all HTTP services as `ClusterIP`; persists metrics, alert state, dashboards,
logs, and Alloy read positions; and applies explicit resource and retention
limits.

## Supply-chain and secrets gate

`versions.env` pins every Helm chart. The values files intentionally contain
`__TVT_REGISTRY__`, `__TVT_HOST_ADDRESS__`, `__TVT_SITE_ID__`,
`__TVT_EDGE_ID__`, and `__*_DIGEST__` tokens.
Installation tooling must render
those tokens from a reviewed image lock containing mirrored `sha256:`
references and must fail if any token, mutable tag-only workload, or unverified
chart archive remains. This repository does not invent registry digests or
store site credentials.

Before installation create these secrets out of band:

- `monitoring/tvt-grafana-admin` with `admin-user` and `admin-password`;
- `monitoring/tvt-alertmanager-webhook` with `token`, matching the protected
  host dispatcher token; and
- Remote Write/Loki mTLS secrets when fleet forwarding is enabled.

Set `TVT_METRICS_LISTEN_HOST` and `TVT_ALERT_LISTEN_HOST` to the dedicated host
address represented by `__TVT_HOST_ADDRESS__`; firewall ports 9108 and 8090 to
the Pod/management networks. The management API itself remains loopback-only.
No Ingress, NodePort, or LoadBalancer is created. Use a reviewed authenticated
management-network proxy if browser access to Grafana is required.

## Apply order

1. Apply `namespace.yaml` and create the two secrets.
2. Render the placeholders and install the pinned
   `kube-prometheus-stack` chart as release `tvt-monitoring` with
   `kube-prometheus-stack.values.yaml`.
3. Install the pinned Loki chart as `tvt-loki` and Alloy chart as `tvt-alloy`
   with their values files.
4. Apply `scrape-configs.yaml`, `pod-monitors.yaml`,
   `grafana-datasources.yaml`, and `tvt-rules.yaml` after the Operator CRDs are
   established.
5. Confirm every target is healthy and every running monitoring image contains
   `@sha256:` before accepting the deployment.

Alloy keeps only `cluster`, `namespace`, `service`, `container`, and `level` as
Loki labels. Its replace stages are a secondary secret/IP safeguard;
applications remain responsible for redaction before stdout. Correlation IDs,
camera IDs, messages, stack traces, timestamps, people, faces, and plates are
never promoted to labels.
