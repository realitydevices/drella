# Drella

<p align="center"><img src="assets/drella.svg" alt="Drella: four pop-art portraits of Andy Warhol in dark sunglasses" width="480"></p>

> *The most important thing is work.*
>
> Andy, as Lou Reed tells it in "Work" (*Songs for Drella*, 1990)

A self-hosted factory where AI agents do the work, on Kubernetes.

You hand Drella work, it sends an agent out to do it, and it keeps a record of what
was done. Agents are short-lived: each one does its job and is gone.

*Drella was Andy Warhol's nickname at the Factory (Dracula + Cinderella).*

**Status:** spec in progress. No code yet. The plan is for Drella to build itself
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
