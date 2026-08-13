# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repository manages a single-node Talos Linux Kubernetes cluster (`k8s-control-1`, VIP `192.168.10.200`) using GitOps via FluxCD. It was bootstrapped with [TrueForge ForgeTool](https://trueforge.org/). The recommended development environment is the provided devcontainer, which pre-installs all required tools (`talhelper`, `flux`, `sops`, `talosctl`, `kubectl`).

## Key Files

- `clusters/main/clusterenv.yaml` — The single source of truth for all cluster variables. **SOPS-encrypted in Git.** Edit this to change IPs, credentials, or add new variables.
- `clusters/main/talos/talconfig.yaml` — Talos machine configuration template; references `${VAR}` variables from `clusterenv.yaml`.
- `clusters/main/talos/generated/` — Generated Talos machine configs and `talosconfig`. Do not edit manually.
- `.sops.yaml` — Defines which files and fields SOPS encrypts.
- `age.agekey` — Local age private key for SOPS decryption (never commit changes to this pattern of file).

## Secret Management (SOPS + age)

All secrets are encrypted with SOPS using age. The rules in `.sops.yaml`:
- `clusterenv.yaml` and `talsecret.yaml` → fully encrypted
- `*.secret.yaml` files in `clusters/main/kubernetes/` → fields matching the sensitive-field regex encrypted
- `authelia-config.yaml` → fully encrypted
- `values-sec.yaml` files → fully encrypted

```bash
# Encrypt a file in-place
sops --encrypt --in-place <file>

# Edit a SOPS-encrypted file
sops <file>

# Decrypt to stdout (without modifying the file)
sops --decrypt <file>
```

The age key must be available at `SOPS_AGE_KEY_FILE` or `~/.config/sops/age/keys.txt`.

## Talos Configuration

Talos machine configs are generated via `talhelper`:

```bash
talhelper genconfig \
  -c clusters/main/talos/talconfig.yaml \
  -s clusters/main/talos/generated/talsecret.yaml \
  -e clusters/main/clusterenv.yaml \
  -o clusters/main/talos/generated/
```

After generating, apply configs with:
```bash
talosctl apply-config --insecure --nodes <NODE_IP> --file clusters/main/talos/generated/<hostname>.yaml
```

## GitOps Architecture (FluxCD)

Flux watches this Git repo and reconciles all resources under `clusters/main/kubernetes/`. Variables in manifests use `${VAR}` syntax — Flux resolves them at runtime from the `cluster-config` ConfigMap (sourced from `clustersettings.secret.yaml`).

The Kubernetes directory is layered with explicit dependencies:

| Layer | Path | Purpose |
|---|---|---|
| `flux-system` | `kubernetes/flux-system/` | FluxCD bootstrap, SOPS secret, GitRepository |
| `kube-system` | `kubernetes/kube-system/` | Core components: Cilium CNI, metrics-server, descheduler, kubelet-csr-approver |
| `system` | `kubernetes/system/` | Platform infra: cert-manager, CloudNative-PG, MetalLB, Longhorn, OpenEBS, kube-prometheus-stack, Spegel, VolSync |
| `core` | `kubernetes/core/` | Cluster services: Blocky DNS, ClusterIssuers, MetalLB config, system-upgrade-controller |
| `networking` | `kubernetes/networking/` | Ingress: traefik-internal/external, nginx-internal/external, Tailscale |
| `apps` | `kubernetes/apps/` | User applications: Authelia, LLDAP, Grafana, Vaultwarden, etc. |

## Adding a New Application

Each app follows this structure:
```
clusters/main/kubernetes/apps/<app-name>/
  ks.yaml               # FluxCD Kustomization (entry point)
  app/
    kustomization.yaml  # Kustomize config
    namespace.yaml      # Namespace (if needed)
    helm-release.yaml   # HelmRelease resource
    values-sec.yaml     # SOPS-encrypted Helm values (if sensitive)
```

1. Create `ks.yaml` pointing to `clusters/main/kubernetes/apps/<app-name>/app`
2. Reference it in `clusters/main/kubernetes/apps/kustomization.yaml`
3. If the app needs secrets, use `${VAR}` substitution from `clusterenv.yaml` or create a `values-sec.yaml` (must be SOPS-encrypted)

## File Conventions

- `.ct` suffix — ForgeTool-managed cluster template files. **Do not edit these directly**; they are regenerated from `clusterenv.yaml`. The comment "DO NOT ALTER THIS FILE, CHANGE DO NOT PERSIST" confirms this.
- `values-sec.yaml` — Fully SOPS-encrypted Helm values for secrets.
- `*.secret.yaml` — SOPS-encrypted Kubernetes Secret manifests.

## Ingress Classes

- `traefik-internal` / `nginx-ingress-internal` — LAN-only (do not port-forward)
- `traefik-external` / `nginx-ingress-external` — Internet-facing (port-forward at router)

## Dependency Update Automation

Renovate is configured via `.github/renovate.json5` extending `github>trueforge-org/renovate-config`. The automerge workflow (`automerge.yaml`) squash-merges Renovate PRs after the placeholder CI passes.
