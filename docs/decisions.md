# Decisions

What's been decided, what's proposed, and what's still open. Other docs build on this
one. When a decision changes, edit it here first.

## Agreed

### What Drella is

1. **The Drella app is the product.** The repo also ships a getting-started substrate
   (`deploy/`) and starter agents and templates (`agents/`). Both work out of the box, but
   they're there to get you going, not the core.
2. Drella runs work for people and for agents. Each piece of work is a **job**: a prompt
   (from a template or ad hoc), input artifacts, output artifacts, and a database record.
3. Each job runs as a **Kubernetes Job**: OpenCode for coding work, Hermes for everything else.
4. **Scheduled agents** = a token + a prompt + memory + a Kubernetes CronJob. The CronJob
   calls the Drella API to create a job, so scheduled and manual jobs follow one path.
5. **Memory** belongs to an agent. The prompt decides what gets remembered ("record what
   you did"). Drella also keeps its own deterministic record (job history, outputs).
6. Works with **any LLM endpoint**: local (e.g. Ollama) or hosted (e.g. Anthropic).
   **Model routing** is set per template, and a job can override it.

### Access

7. **Token auth.** Every request belongs to a token.
8. **One API.** The HTTP API is the only way in. The web UI, the MCP server, the CLI and
   CronJobs are all clients of it, and nothing gets a private side door.
9. Drella **exposes an MCP server** so other agents can hand out work and check on it.
   It doesn't consume MCP (job agents may get MCP connections later).
10. **Credentials** (e.g. git credentials) are stored encrypted, per token. They're only
    ever decrypted into the pod for the job that needs them.
11. **The agent opens its own PR.** Drella doesn't push on its behalf. Repos rely on branch
    protection, so an agent can push branches and open PRs but can't merge to `main`.
12. **Users:** several. Every non-human token (an agent) has an **owner**, a human token.
    An admin role comes later.

### Platform

13. **Kubernetes**, **Postgres**, and **object storage**. Storage sits behind a small
    interface (put / get / list / delete, streamed). It ships with two implementations:
    S3-compatible and local filesystem. Job pods never talk to storage directly: they go
    through the Drella API, so the storage backend can be swapped.
14. **Python.** FastAPI, SQLAlchemy + Alembic, and `uv` for packaging.
15. **Deploy:** plain manifests with kustomize. No Helm.
16. Getting-started object storage: **Garage** (small, single binary, S3 API).
17. The job launcher sits behind a small **runner interface**. Kubernetes is the only
    implementation for now. Docker is excluded as the main target, because socket access
    is root on the host.

### Jobs

18. **Tokens:** random, shown once, stored hashed. Scopes come later.
19. **Job pod contract:**
    - **Before the agent:** inputs land in `/in`, credentials are written to files, and memory is fetched.
    - **The agent:** writes results to `/out`.
    - **Status:** reported to the API using a token scoped to that one job.
    - **Live logs:** you can follow a job's logs as it runs.
    - **If the job dies:** nothing it had already written is lost (see P3).
20. **Queues:** endpoints and models belong to **queue groups**, each with its own
    concurrency limit. For example, `local` runs 1 at a time and `anthropic` runs 4. Jobs
    queue FIFO within a group.
21. **Default limits:** 1 hour timeout, 2 GB RAM per job. Templates can override both.

### Process

22. Repos are public, and `main` is protected (PR + 1 review).
23. The docs in `docs/` are the spec, written for people and for the agents implementing them.
24. **v0 is built by hand.** It's just enough to run one job: token → create job (prompt +
    repo) → OpenCode against a local model → log + output artifact. No UI, schedules,
    memory or MCP yet. From then on Drella builds itself from these docs.

## Proposed (accept, change or reject)

- **P1. One job image.** `drella-job` contains OpenCode, Hermes and the job wrapper; the
  job's command picks the agent. That's one image to build, version and pull. Split it
  only if the size or dependency conflicts start to hurt.
- **P2. Web UI without a build step:** static HTML plus a small JS library (e.g. Alpine.js)
  calling the JSON API. HTMX is out because it wants HTML responses, and the API returns JSON (see 8).
- **P3. Nothing lost when a job dies.** Each job pod gets a **shipper** sidecar beside the
  agent. The sidecar:
  - streams log lines to the API every second or so; the API keeps them and serves
    `GET /jobs/{id}/logs?follow=1` as server-sent events;
  - uploads `/out` and `/memory` whenever they change, and again on exit.

  If the agent is OOM-killed, the sidecar still finishes uploading. If the whole node
  dies, you lose only the last few seconds. Coding agents also push their branch early and often.
- **P4. Token-derived credential keys.** Credentials are encrypted with a key derived from
  the token, so only someone holding the token can decrypt them. Neither a database dump
  nor the server can. Two values come from the token, and they must be different:
  - the lookup hash, `sha256(token)`, which is stored;
  - the encryption key, `HKDF(token, salt, "creds")`, which is never stored.

  Credentials get decrypted when a job is created (the request carries the token) and are
  put into a Secret for that job only. Trade-offs:
  - A lost token means re-entering its credentials.
  - Rotating a token re-encrypts them.
  - An owner sets an agent's credentials when creating the agent.
- **P5. Memory is versioned inside Drella.** Each agent has a folder of markdown files.
  Every job that changes memory saves a new version, so you get history and diffs without
  GitHub. Memory stays behind an interface, so a vector store can be added later. An
  optional "mirror to a git repo" exporter can come later too. GitHub isn't the default
  because memory may hold private details and repos are public (22).
- **P6. Agent identity on GitHub:** an agent that should post "as itself" gets its own
  GitHub App. Apps are free, and an App's credentials are stored like any other credential.
  Agents without their own App use their owner's credentials.

## Open

- **O1. Exposure:** how Drella is reached from outside the cluster (ingress, TLS). The
  getting-started deploy is LAN-only; production setups vary.
- **O2. Retention:** how long job logs, artifacts and memory versions are kept.
