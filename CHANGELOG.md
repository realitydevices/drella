# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.1.0] — 2026-11-10

### Added

- **FastAPI API** with endpoints for credentials, jobs, logs, outputs, and health
- **Token-based auth** and encrypted credential storage
- **Postgres-backed storage** with a local filesystem fallback
- **Kubernetes job runner** that executes AI agent tasks as short-lived pods
- **Queue loop** with concurrency control and crash recovery
- **CLI** (`drella`) for token management
- **Kustomize manifests** for deploying to k3s or any Kubernetes cluster
- **Getting-started guide** (`deploy/README.md`) for cluster setup
- **v0 spec** (`docs/v0.md`) covering cluster, API, job image, reliability, and security
- **Decisions doc** (`docs/decisions.md`) recording architectural choices
- **GitHub Actions CI** to build and publish images on main and test on PRs
- **Tests** for crypto, storage, API, queue, and runner modules

[Unreleased]: https://github.com/realitydevices/drella/compare/v0.1.0...HEAD
[v0.1.0]: https://github.com/realitydevices/drella/releases/tag/v0.1.0
