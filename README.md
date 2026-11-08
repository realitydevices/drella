# Mortville

A self-hosted town where AI agents live and work, on Kubernetes.

You hand Mortville work, it sends a resident out to do it, and it keeps a record of
what was done. Residents are short-lived: each one does its job and is gone.

*Named for the town in John Waters' *Desperate Living* (1977).*

**Status:** spec in progress. No code yet. The plan is for Mortville to build itself
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
