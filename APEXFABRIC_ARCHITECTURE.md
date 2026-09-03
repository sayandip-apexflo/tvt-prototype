# ApexFabric prototype architecture

This guide explains the repository without assuming Kubernetes experience. It
starts with the basic vocabulary, follows a node and a Solution Pack through the
system, and ends with the current security and production boundaries.

## The short version

ApexFabric describes what should run and where it is allowed to run. Kubernetes
decides which eligible computer runs it, starts the containers, checks their
health, and replaces them after failures.

The repository separates four responsibilities:

1. A **node reporter** runs on each compute node and reports observations.
2. A **node-status controller** validates reports and owns scheduling labels.
3. A **Solution Pack reconciler** converts an accepted bundle into Kubernetes
   resources.
4. **Kubernetes/K3s** schedules and operates the resulting workloads.

The browser UI is a development tool layered over this flow. It is not part of
the intended production control plane.

Its demo stop/reset actions use an explicit resource allowlist. They scale or
recreate fake CV, the event receiver and demo Prometheus only; they do not stop
K3s, node reporters, the node-status controller or unrelated workloads.

## Kubernetes vocabulary used here

| Term | Plain-language meaning |
|---|---|
| Cluster | All server and compute machines managed as one system |
| K3s server | Runs the Kubernetes API and scheduler; stores cluster desired state |
| K3s agent | Runs on a compute machine and starts containers assigned to that machine |
| Node | Kubernetes's record for one server or compute machine |
| Container image | Packaged application filesystem and startup command |
| Pod | One scheduled instance of one or more containers |
| Deployment | Desired replica count and update strategy for Pods |
| ReplicaSet | Deployment-owned controller that maintains one Pod template revision |
| Service | Stable network name that routes only to eligible, Ready Pods |
| Probe | HTTP check used by kubelet to determine startup, readiness or liveness |
| Label | Key/value metadata used by the scheduler, such as architecture or camera access |
| Affinity | Rules saying which labelled nodes a Pod may use |
| DaemonSet | Ensures one Pod runs on every matching node |
| CRD | A custom Kubernetes record type supplied by ApexFabric |
| Controller | A loop that observes current state and moves it toward desired state |
| Reconcile | Compare desired and actual state, then apply only required changes |

K3s is a compact Kubernetes distribution. The Kubernetes behaviors discussed
in this repository—Deployments, Services, probes, scheduling and RBAC—are not
K3s-specific concepts.

## System overview

```mermaid
flowchart TB
    User[Developer or operator] --> UI[Dev-only local UI<br/>127.0.0.1:8088]
    UI --> HostControl[Host control service]

    subgraph Cluster[K3s cluster]
        API[Kubernetes API]
        Scheduler[Default scheduler]
        NodeController[Node-status controller]
        Reconciler[Solution Pack reconciler]

        subgraph Compute[Each enabled compute node]
            Reporter[Node reporter DaemonSet Pod]
            Kubelet[K3s agent and kubelet]
            Runtime[containerd]
            Workload[Solution workload Pod]
        end

        Reporter -->|ApexNodeStatus report| API
        API --> NodeController
        NodeController -->|validated Node labels| API
        Reconciler -->|Deployments, Services,<br/>ConfigMaps and Secrets| API
        API --> Scheduler
        Scheduler -->|Pod assignment| Kubelet
        Kubelet --> Runtime
        Runtime --> Workload
    end

    HostControl -->|development API operations| API
    UI -. visibility only .-> API
```

The arrows matter:

- The reporter does **not** label nodes or deploy applications.
- The node-status controller does **not** deploy Solution Packs.
- The reconciler does **not** select a concrete node.
- The scheduler does **not** decide desired application configuration.

## What runs where

```mermaid
flowchart LR
    subgraph ServerHost[K3s server machine]
        K3sServer[K3s server]
        DevUI[Dev UI and host controller]
        Prometheus[Demo Prometheus]
    end

    subgraph KubernetesControl[Server-side Kubernetes workloads]
        StatusController[Node-status controller]
    end

    subgraph NodeA[Compute node A]
        AgentA[K3s agent]
        ReporterA[Node reporter]
        AppA[Scheduled app Pod]
    end

    subgraph NodeB[Compute node B]
        AgentB[K3s agent]
        ReporterB[Node reporter]
        AppB[Scheduled app Pod]
    end

    K3sServer <--> AgentA
    K3sServer <--> AgentB
    ReporterA --> K3sServer
    ReporterB --> K3sServer
    StatusController --> K3sServer
    DevUI --> K3sServer
    Prometheus --> AppA
    Prometheus --> AppB
```

On the development laptop, the K3s server and one compute node are the same
physical machine. Keeping the logical roles separate makes the same model
extend to additional compute boxes later.

Server-only management workloads select `apexfabric.com/control-plane=true`.
The installer applies that label to the local server by matching its machine
ID, preventing a controller whose image exists only on the server from being
scheduled onto an agent.

An additional compute box runs `k3s-agent` only. It must not simultaneously run
`k3s server`; both roles attempt to own the local agent load-balancer port
`6444`. The supported mixed cluster uses multi-architecture images pushed to
the laptop's private Docker Registry and pulled by Intel 285H and Jetson Orin
workers.

## Installation and startup

The supported `scripts/install-server.sh` wrapper establishes the stable server
identity, invokes the K3s and platform installers, and publishes the
multi-architecture management/demo images. `install-agent.sh` provisions and
joins one worker at a time. The component scripts remain useful for repair and
development, but [CLEAN-INSTALL.md](CLEAN-INSTALL.md) is the installation
runbook.

```mermaid
sequenceDiagram
    actor Operator
    participant Installer as install-server.sh
    participant Host as Linux host
    participant API as Kubernetes API
    participant K3s as K3s/containerd
    participant Systemd

    Operator->>Installer: Run with sudo
    Installer->>Host: Apply packages, modules and sysctls
    Installer->>API: Apply namespace and RBAC
    Installer->>K3s: Configure HTTP registry
    Installer->>Host: Build and push reporter/controller images
    Installer->>API: Apply CRD, DaemonSet and controller Deployment
    Installer->>API: Enable reporter on this local Node
    Installer->>Systemd: Install and start dev UI, host audit and port forwards
    API-->>Operator: Workloads converge asynchronously
```

The install is split this way because host provisioning needs root access,
while normal cluster reconciliation should happen through narrowly scoped
Kubernetes service accounts.

## Node discovery and labelling

The reporter creates an `ApexNodeStatus` custom resource. It contains observed
CPU architecture, memory, storage, network, decoder, accelerator and configured
camera information. It does not claim scheduling authority.

The reporter also reasserts the qualified static
`apexfabric.com/camera-streams` extended resource for nodes carrying a supported
`apexfabric.com/hardware-profile` label. This capacity comes from the shared
hardware-profile configuration rather than runtime benchmarking or camera
discovery.

```mermaid
sequenceDiagram
    participant Reporter as Node reporter
    participant Host as Mounted host interfaces
    participant API as Kubernetes API
    participant Controller as Node-status controller
    participant Node as Kubernetes Node

    loop Every 30 seconds
        Reporter->>Host: Read discovery inputs
        Reporter->>API: Create or patch ApexNodeStatus
    end
    Controller->>API: List reports and Nodes
    Controller->>Controller: Check freshness, readiness,<br/>node identity and architecture
    alt Report accepted
        Controller->>Node: Patch approved scheduling labels
        Controller->>API: status.accepted = true
    else Report invalid or stale
        Controller->>Node: qualified = false
        Controller->>API: Record rejection reason
    end
```

Examples of controller-owned labels are:

```text
apexfabric.com/qualified=true
apexfabric.com/node-class=cv
apexfabric.com/architecture=amd64
apexfabric.com/decoder=vaapi
apexfabric.com/metis=false
```

This separation prevents a node process from declaring itself qualified. The
current prototype still needs admission controls and stronger per-node identity
before this trust boundary is production-grade.

## Solution Pack deployment and scheduling

A DeploymentBundle says what applications should exist, their images,
configuration, probes, resource requests, required cameras and placement
characteristics. It does not name a specific compute node.

```mermaid
sequenceDiagram
    actor User
    participant UI as Dev UI
    participant Validator
    participant Reconciler as Solution Pack reconciler
    participant API as Kubernetes API
    participant Scheduler
    participant Agent as K3s agent
    participant Registry as Registry or local image store

    User->>UI: Enter product intent or upload a DeploymentBundle
    UI->>UI: Generate canonical bundle and preview objects
    UI->>Validator: Validate schema and semantics
    Validator-->>UI: Accepted
    UI->>Reconciler: Reconcile desired state
    Reconciler->>API: Apply Deployment, Service,<br/>ConfigMap, Secret, PVC and policy
    API->>Scheduler: Unscheduled Pod appears
    Scheduler->>Scheduler: Compare requests and affinity<br/>with Ready Node labels
    Scheduler->>Agent: Bind Pod to eligible Node
    Agent->>Registry: Fetch image if absent
    Agent->>Agent: Start container and run probes
    Agent-->>API: Report Pod status
    API-->>UI: Show Pending, Running or Ready
```

If no node satisfies resources or affinity, the Pod remains `Pending` and the
scheduler records a reason. It is safer to leave a workload visibly Pending
than to run it on unsupported hardware.

## Configuration and runtime data paths

```mermaid
flowchart LR
    Bundle[DeploymentBundle] --> Validator[Bundle validator]
    Validator --> Reconciler[Reconciler]
    Reconciler --> CM[ConfigMap<br/>non-secret configuration]
    Reconciler --> Secret[Secret<br/>prototype credentials]
    Reconciler --> PVC[PersistentVolumeClaim<br/>retained application data]
    Reconciler --> Deployment[Deployment]
    CM --> Pod[Application Pod]
    Secret --> Pod
    PVC --> Pod
    Deployment --> Pod
    Pod --> Events[Analytics event HTTP queue]
    Events --> Sink[Demo event sink]
    Pod --> Metrics[Metrics endpoint]
    Metrics --> Prom[Prometheus]
```

The fake CV workload reads configuration from mounted files and Secret-backed
environment variables. Production must replace plaintext bundle secret values
with references to a proper secret-management workflow.

## Health and failure recovery

Kubernetes uses three distinct probe meanings:

```mermaid
flowchart TD
    Startup[startupProbe] -->|fails during startup| Wait[Keep waiting until threshold]
    Startup -->|passes| Live[livenessProbe]
    Live -->|fails repeatedly| Restart[Kubelet restarts container]
    Ready[readinessProbe] -->|fails| Remove[Pod keeps running<br/>Service removes ready endpoint]
    Ready -->|recovers| Restore[Service restores endpoint]
    Delete[Pod deleted or lost] --> RS[ReplicaSet notices missing replica]
    RS --> Replacement[Replacement Pod]
```

The repository exercises these behaviors against real K3s in
`scripts/test-k8s-lifecycle.sh`. See `FAILURE-RECOVERY.md` for exact assertions
and captured evidence.

## Monitoring path

```mermaid
flowchart LR
    App[Fake CV Pod] -->|HTTP /health| Kubelet
    App -->|HTTP /ready| Kubelet
    App -->|Prometheus text /metrics| Prometheus
    Reporter[Node reporter] -->|/health and /metrics on 9100| NodeOps[Node operations]
    Prometheus -->|port forward 19090| Browser[Developer browser]
    App -->|port forward 18080| Browser
    API[Kubernetes API] --> UI[Dev UI status]
```

The current Prometheus manifest directly scrapes the fake CV workload and uses
ephemeral storage. Reporter metric discovery, durable monitoring, alert rules
and long-term retention remain production work.

## Pull-based OCI image promotion

The central controller has internet access, but its OCI registry remains a
private service. New application images therefore move into the registry using
a **pull-based promotion** flow: the controller fetches an approved image from
an upstream registry instead of allowing an internet-hosted CI runner to push
directly into the private registry.

An OCI registry is a store for versioned container images. A tag such as
`2026.09.03-v5` is a readable name, while a digest such as `sha256:...` is the
immutable fingerprint of the image contents. Promotion and deployment use the
digest so that a tag cannot silently change which software runs at the edge.

```mermaid
flowchart LR
    Developer[Developer] -->|code change| CI[CI pipeline]
    CI -->|build, test, scan and sign| Staging[Upstream OCI registry]
    Staging -->|approved repository@sha256 digest| Promotion[Controller promotion worker]
    Promotion -->|Skopeo copy| Registry[Controller private OCI registry]
    Promotion -->|verify digest and write atomically| Lock[Image lock and catalog]
    Lock -->|separate deployment approval| Reconciler[Solution Pack reconciler]
    Reconciler -->|digest-pinned Pod specification| API[Kubernetes API]
    Registry -->|private-network image pull| Agent[K3s agent]
    API -->|schedule Pod| Agent
```

The flow has two deliberately separate decisions:

1. **Promote:** make a verified image available in the controller registry.
2. **Deploy:** change a workload to use that image's immutable digest.

Separating these decisions prevents a successful image upload from
automatically changing a running workload.

### Promotion sequence

```mermaid
sequenceDiagram
    actor Approver
    participant CI as CI pipeline
    participant Upstream as Upstream OCI registry
    participant Worker as Controller promotion worker
    participant Registry as Private OCI registry
    participant Catalog as Image lock and solution catalog

    CI->>CI: Build, test, inspect and scan image
    CI->>Upstream: Push immutable release candidate
    Upstream-->>CI: Return source digest
    CI->>CI: Create signed promotion manifest
    Approver->>CI: Approve exact source digest
    Worker->>CI: Fetch approved promotion manifest
    Worker->>Upstream: Authenticate with read-only credentials
    Worker->>Registry: Skopeo copy source@digest to versioned tag
    Worker->>Registry: Read back destination manifest and digest
    alt Verification succeeds
        Worker->>Catalog: Atomically record digest-pinned image lock
        Catalog-->>Approver: Image is available for deployment
    else Copy or verification fails
        Worker->>Worker: Retain previous lock and report failure
    end
```

The promotion manifest is a small signed document containing the exact source
registry, repository and digest; destination repository and versioned tag;
target architecture; release identity; and relevant contract checksums. The
controller accepts a promotion only after CI qualification and an explicit
production approval.

The controller-side worker uses Skopeo because it can copy directly between
OCI registries without starting the image or requiring access to the Docker
daemon. It pulls the source by digest, retries bounded network failures,
records the destination digest and reads the destination manifest back. It
rejects attempts to move an existing production tag to different content.

During the current single-node phase, Skopeo can write to the loopback registry
at `127.0.0.1:5000`. A multi-node installation must additionally publish a
stable private-network registry hostname for K3s nodes, protected by TLS,
pull-only node credentials and firewall rules. Every server and agent then
uses that hostname and trusted CA in `/etc/rancher/k3s/registries.yaml` and
pulls workload images by digest.

The production security boundary is:

- the public CI runner never receives direct network access to the private
  registry;
- the controller initiates outbound HTTPS connections to CI and the upstream
  registry;
- upstream credentials are read-only, short-lived where possible and never
  included in command arguments or logs;
- only a dedicated promotion identity may add images or update the image lock;
- existing images are retained so a deployment can roll back to its previous
  digest; and
- copying an image never generates a DeploymentBundle, calls K3s or changes a
  running workload.

Direct remote pushes are an optional, less-preferred topology. They require a
private VPN or equivalent network path, TLS, authenticated and scoped push
credentials, firewall allowlisting, audit logging and a separate threat
review. Publishing the current unauthenticated loopback registry directly to
the internet is not supported.

## Offline compute nodes

Compute nodes do not require Wi-Fi or internet. They require a private network
path to the K3s server and a way to obtain signed binaries and container images.

```mermaid
flowchart LR
    CI[Internet-connected build/release system] -->|candidate image and signed manifest| Upstream[Upstream OCI registry]
    Upstream -->|controller-initiated Skopeo pull| Server[K3s server and private registry]
    CI -.->|signed offline kit fallback| Media[Controlled media or staging laptop]
    Media -.-> Server
    Server -->|K3s API 6443| Agent1[Offline compute node]
    Server -->|images over private Ethernet| Agent1
    Server -->|K3s API 6443| Agent2[Offline compute node]
    Server -->|images over private Ethernet| Agent2
    Agent1 <-->|cluster networking| Agent2
```

The current repository imports and publishes images locally. The pull-based
Skopeo promotion worker described above is the target production ingestion
path and is not automated yet. When the controller also lacks internet access,
the offline installer remains the fallback: it must distribute signed,
verified OCI archives and securely deliver an expiring K3s join credential and
the architecture-matching K3s air-gap bundle.

The demo manifest also supplies one shared camera ConfigMap to reporters. A
production multi-node system needs authenticated per-node camera reachability
configuration and active connectivity checks; it must not assume every node can
reach the same cameras.

## Permissions and trust boundaries

| Component | Allowed responsibility | Must not do |
|---|---|---|
| Node reporter | Create/update capability reports | Label Nodes, deploy apps, read application Secrets |
| Node-status controller | Read reports/Nodes, update report status and approved Node labels | Deploy Solution Packs |
| Solution Pack reconciler | Manage owned namespace application resources | Select `nodeName`, modify arbitrary Nodes |
| Image promotion worker | Pull an approved source digest, verify it and atomically update the image lock/catalog | Execute an image, deploy a workload, delete old images or accept an unapproved tag |
| Workload Pod | Read its own mounted configuration/Secret and serve its ports | Access Kubernetes API or host filesystem |
| Dev UI/controller | Development orchestration and visibility | Be exposed as a production unauthenticated service |

The manifests implement these as separate service accounts and RBAC roles. The
remaining production gaps include per-node report authorization, admission
policy, automated signature enforcement, the Skopeo promotion worker,
controller leader election, audit hardening, TLS/user authentication and
secret-store integration.

## Where to read next

- `README.md`: install commands, repository map and production boundary.
- `PLATFORM.md`: operational behavior and V1 goal coverage.
- `FAILURE-RECOVERY.md`: reproducible Kubernetes failure tests.
- `UPGRADE-MODEL.md`: external configuration, rolling upgrade and data-lifecycle boundary.
- `deploy/k8s/apexfabric-node-management.yaml`: CRD, DaemonSet, controller and RBAC.
- `solution-packs/schema/deployment-bundle.schema.json`: accepted Solution Pack structure.
