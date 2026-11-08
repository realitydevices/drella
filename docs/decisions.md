# Decisions

What's been decided, what's proposed, and what's still open. Other docs build on this
one. When a decision changes, edit it here first.

## Agreed

**Shape**
1. Drella runs work for people and for agents. Each piece of work is a **job**: a prompt
   (from a template or ad hoc), input artifacts, output artifacts, and a database record.
2. Each job runs as a **Kubernetes Job**: OpenCode for coding work, Hermes for everything else.
3. **Scheduled agents** = a token + a prompt + memory + a Kubernetes CronJob. The CronJob
   calls the Drella API to create a job, so scheduled and manual jobs follow one path.
4. **Memory** belongs to an agent. The prompt decides what gets remembered ("record what
   you did"). Drella also keeps its own deterministic record (job history, outputs).
5. Works with **any LLM endpoint**: local (Ollama) or hosted (Claude).

**Access**
6. **Token auth.** Every request belongs to a token.
7. **HTTP API + web UI.**
8. Drella **exposes an MCP server** so other agents can hand out work and check on it.
   It doesn't consume MCP (job agents may get MCP connections later).
9. **Credentials** (e.g. a GitHub PAT) are stored encrypted, per token. They're only ever
   decrypted into the pod for the job that needs them.
10. **The agent opens its own PR.** Drella doesn't push on its behalf.

**Platform**
11. **Kubernetes** (k3s on the geekom), **Postgres**, **S3-compatible blob storage**.
12. **Python.**
13. Three layers:
    - **Substrate:** k8s + Postgres + storage. The repo ships a lightweight setup for these (`deploy/`).
    - **App:** the `drella` image (API, UI, MCP) and the job images (`images/`).
    - **Agents:** prompts, templates and agent definitions (`agents/`). These are data
      loaded into Drella, not code.
14. The job launcher sits behind a small **runner interface**. Kubernetes is the only
    implementation for now. Docker is excluded as the main target, because socket access
    is root on the host.

**Process**
15. The repo is public, and `main` is protected (PR + 1 review).
16. The docs in `docs/` are the spec, written for people and for the agents implementing them.
17. **v0 is built by hand** (Kirk + Claude), and is just enough to run one job. From then on
    Drella builds itself from these docs.

## Proposed (accept, change or reject)

- **P1. Blob store:** Garage (small, single binary, S3 API). MinIO is winding down its
  free edition; SeaweedFS is the fallback.
- **P2. Python stack:** FastAPI, SQLAlchemy + Alembic, `uv` for packaging.
- **P3. Web UI:** server-rendered HTML + HTMX. No JS build step.
- **P4. Deploy:** plain manifests with kustomize. No Helm.
- **P5. Tokens:** random, shown once, stored hashed. Scopes come later.
- **P6. Credential encryption:** Fernet, with the master key in a k8s Secret.
- **P7. Job pod contract:**
  - **Before the agent:** a wrapper downloads inputs to `/in`, writes credentials to
    files, and fetches the agent's memory.
  - **The agent:** runs and writes results to `/out`.
  - **After the agent:** the wrapper uploads `/out` and the updated memory, then
    reports status to the API using a token scoped to that one job.
- **P8. Memory:** a folder of markdown files per agent in the blob store, mounted at
  `/memory` during the job.
- **P9. Concurrency:** one GPU means one local-model job at a time. Drella queues jobs
  and runs them FIFO, with a per-endpoint concurrency limit (default 1).
- **P10. Network:** LAN only for v0. Caddy on the geekom proxies to the k3s service later.

## Open

- **O1. PR enforcement:** a bot account that can only push to a fork and open PRs. How
  does the job get the bot's credential, and who sets up the fork?
- **O2. Who uses it:** just Kirk and his agents, or several people? This decides
  whether tokens need owners/users above them.
- **O3. Job limits:** default timeout, CPU/memory per job, and how long logs and artifacts are kept.
- **O4. v0 scope:** proposed as token → create job (prompt + repo) → OpenCode against
  Ollama → log + output artifact. No UI, schedules, memory or MCP yet.
- **O5. Model routing:** how a job picks its endpoint/model: per job, per template, or per agent.
