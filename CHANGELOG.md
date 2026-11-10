# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [v0.2.0] — 2026-11-10

### Added

- **Models table**: register models by alias instead of raw ID + endpoint; jobs name an alias
- **API key support** for hosted model endpoints (e.g. Anthropic), stored as encrypted credentials
- **Adapters** for `openai-compatible` and `anthropic` interfaces — produce OpenCode config and preflight checks
- **Per-token models**: each token sees only its own models; shared ("house") models come later with admin roles
- **Model snapshots** on job creation: the job records a snapshot of the model at launch time
- **Validation**: stricter `base_url` and `model_id` rules; caps on models per token (200); 422 errors never echo credentials
- **Key redaction**: the wrapper replaces API key values with `[redacted]` in the transcript and session JSON (8+ char keys)
- **git credential helper** scoped to `https://github.com` only
- **Security**: model rows can't name the `github` credential; keyed models require `https`; OpenCode project config disabled (`OPENCODE_DISABLE_PROJECT_CONFIG=1`)
- **Jobs without a repo** now fail if they produced no outputs (only regular files, not symlinks, at the top of `/out`)
- **Migration**: existing v0.1 jobs keep their model ID and endpoint as a snapshot with interface `openai-compatible`

## [v0.1.0] — 2026-11-10

### Added

- **FastAPI API** with endpoints for credentials, jobs, logs, outputs, and health
- **Token-based auth** and encrypted credential storage
- **Postgres** for metadata (jobs, models, credentials, tokens); **local filesystem storage** for job logs and outputs (`drella/storage.py`)
- **Kubernetes job runner** that executes AI agent tasks as short-lived pods
- **Queue loop** with concurrency control (concurrency 1) and crash recovery
- **CLI** (`drella`) for token management
- **Kustomize manifests** for deploying to k3s or any Kubernetes cluster
- **Getting-started guide** (`deploy/README.md`) for cluster setup
- **v0 spec** (`docs/v0.md`) covering cluster, API, job image, reliability, and security
- **Decisions doc** (`docs/decisions.md`) recording architectural choices
- **GitHub Actions CI** to build and publish images on main and test on PRs
- **Tests** for crypto, storage, API, queue, and runner modules

### Time limits and logs

- **Soft limit**: wrapper stops the agent 5 minutes before the hard deadline
- **Hard limit**: Kubernetes `activeDeadlineSeconds` (1 hour) as a backstop
- **Log archiving**: pod logs copied to storage every 30 seconds; survives pod deletion
- **Log rotation** handling: detects kubelet rotation and stitches segments together

### Reliability

- Jobs don't depend on the API being up during execution (retries on status/report at end)
- Bad jobs can't stall the queue (per-job error handling, timeouts on every Kubernetes call)
- Liveness probe restarts a stuck API process
- Safe restarts: adopts in-flight Jobs; cleans up abandoned Secrets

### Security

- **Credentials scoped**: each job gets only the credentials it needs (`github` for repo jobs, model key if required)
- **NetworkPolicy** for job pods: no inbound; outbound limited to DNS, the API, the model endpoint, and the public internet
- **Pod hardening**: non-root, no privilege escalation, all capabilities dropped, seccomp profile, no service account token
- **Branch protection requirements**: default branch must require PRs, at least one approval, no force pushes; release tags must be protected

### Releases

- Images pinned to git tags; pushing `vX.Y.Z` publishes `drella:vX.Y.Z` and `drella-job:vX.Y.Z`
- `drella` and `drella-job` images always share the same tag
- CI builds on PRs but only publishing branches get write access to GHCR packages

[Unreleased]: https://github.com/realitydevices/drella/compare/v0.2.0...HEAD
[v0.2.0]: https://github.com/realitydevices/drella/releases/tag/v0.2.0
[v0.1.0]: https://github.com/realitydevices/drella/releases/tag/v0.1.0
