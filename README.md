# Lumbergh

> "Yeah… if you could just go ahead and get that done, that'd be great."

Lumbergh is a self-hosted runner that delegates work to AI agents on Kubernetes.
You hand it work, it hands the work to an agent, and it keeps a record of what was
done.

**Status:** spec in progress. No code yet. The plan is for Lumbergh to build itself
from these docs.

## The idea

- **Jobs:** a prompt (from a template or ad hoc) plus input artifacts, run as a
  Kubernetes Job by an agent (OpenCode for coding, Hermes for everything else),
  producing output artifacts and a session record.
- **Agents:** a token, a prompt, a place to keep memory and a schedule. A CronJob
  wakes the agent and it picks up where it left off.
- **Access:** token-based HTTP API, a web UI, and an MCP server so other agents
  (Claude, Hermes, …) can hand out work and check on it.
- **Credentials:** stored encrypted, per token.
- **Models:** any LLM endpoint: local (Ollama) or hosted (Claude).
- **Runs on:** Kubernetes (k3s is fine), Postgres, and S3-compatible blob storage.

## Docs

Specs live in [`docs/`](docs/). They're written to be read by people *and* by the
agents that will implement them.
