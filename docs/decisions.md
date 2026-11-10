# Decisions

What's been decided and what's still open. Other docs build on this
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
   Memory is a folder of markdown files per agent, kept inside Drella and versioned: every
   job that changes it saves a new version, so you get history and diffs. It sits behind an
   interface, so a vector store or a mirror to a git repo can be added later. It isn't kept
   on GitHub by default, because memory may hold private details and repos are public.
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
    An agent that should post "as itself" gets its own GitHub App, whose credentials are
    stored like any other. Agents without one use their owner's credentials.
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
17. **Exposure:** Drella exposes one HTTP service from the cluster. Whatever sits in front
    of it (a reverse proxy, an ingress, DNS, TLS) is the operator's choice. The
    getting-started docs show one example.
18. The job launcher sits behind a small **runner interface**. Kubernetes is the only
    implementation for now. Docker is excluded as the main target, because socket access
    is root on the host.

### Jobs

19. **Tokens:** random and high-entropy, shown once, never stored. Scopes come later.
    Two separate values are derived from each token:
    - **lookup hash** `sha256(token)`, which is stored and used to find the token;
    - **encryption key** `HKDF(token, salt, "creds")`, which is never stored and encrypts
      that token's credentials (Fernet).

    Only someone holding the token can decrypt its credentials; a database dump or the
    server on its own can't. Credentials are decrypted when a job is created (the request
    carries the token) and put into a Secret for that job only. Trade-offs: a lost token
    means re-entering its credentials, rotating a token re-encrypts them, and an owner sets
    an agent's credentials when creating the agent.
20. **One job image.** `drella-job` contains OpenCode, Hermes and the job wrapper; the
    job's command picks the agent. Split it only if size or dependency conflicts start to hurt.
21. **Job pod contract:**
    - **Before the agent:** inputs land in `/in`, credentials are written to files, and memory is fetched.
    - **The agent:** writes results to `/out`.
    - **Status:** reported to the API using a token scoped to that one job.
    - **On exit:** the wrapper uploads `/out` and memory.
    - **Logs** come straight from Kubernetes. The API follows the pod's logs for live tail
      (`GET /jobs/{id}/logs?follow=1`). Drella copies the log to object storage while the
      job runs and when it finishes, because pod logs disappear with the pod (limits in
      [v0.md](v0.md#time-limits-and-logs)).
    - **If the agent is killed,** its unsaved work may be lost, but the log survives, which
      is enough to diagnose and fix. Coding agents push their branch early and often.
22. **Queues:** endpoints and models belong to **queue groups**, each with its own
    concurrency limit. For example, `local` runs 1 at a time and `anthropic` runs 4. Jobs
    queue FIFO within a group.
23. **Default limits:** 1 hour timeout, 2 GB RAM per job. Templates can override both.

### Process

24. Repos are public, and `main` is protected (PR + 1 review).
25. The docs in `docs/` are the spec, written for people and for the agents implementing them.
26. **v0 is built by hand.** It's just enough to run one job: token → create job (prompt +
    repo) → OpenCode against a local model → log + output artifact. No UI, schedules,
    memory or MCP yet. From then on Drella builds itself from these docs.

### Models

27. **Models are registered, and jobs name them by alias** ([v0.2](v0.2.md)). One table:
    alias, interface, base URL, model ID, an optional credential name for the API key,
    context length and costs. The **interface** (e.g. `openai-compatible`, `anthropic`)
    picks an **adapter** in code, which renders the agent's provider config and the
    preflight check; adding a provider type is code, adding a model is a row. The key
    comes from the caller's own credentials [19] and reaches the pod only as an env var.
    Each job keeps a snapshot of its model. **Models are per token until O2:** a token
    sees, changes, deletes and uses only the models it registered, and two tokens can
    each have their own model with the same alias. Whoever controls a model's endpoint
    controls the agent, which holds the job's GitHub token, so a shared model would hand
    another token's jobs to its owner. Shared ("house") models come with O2's roles. A
    model can't name the `github` credential, a model with a key needs `https`, and
    `base_url` and `model_id` are printable ASCII with no `{` or `}` (OpenCode
    substitutes `{env:…}` and `{file:…}` in its config); a stored model is checked again
    when a job uses it. OpenCode never reads config from the repo.

## Open

- **O1. Retention:** how long job logs, artifacts and memory versions are kept.
