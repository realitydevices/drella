# Automatic releases and deploys

Status: discussed (2026-11-10), not agreed.

Today a release is a tag someone pushes, and a deploy is someone running
`kubectl apply -k` against a local overlay. Both need a person each time.

## Proposal

**Releasing: release-please.** A GitHub Action keeps a standing release PR on `main`
that bumps the version and writes CHANGELOG.md from the merged commits. Merging it
is the release decision: it tags `vX.Y.Z`, and the images publish as now. One human
gate, one approval.

**Deploying: a small deployer in the cluster.** A CronJob, every few minutes:
1. finds the newest `v*` tag whose CI passed;
2. if it's newer than what's running, applies `deploy/` at that tag;
3. smoke-tests it with a real Drella job (no repo, a local model, "write ok to
   /out/ok") and waits for it to succeed;
4. rolls back to the previous tag if the rollout or the smoke test fails, and
   records what happened.

v0.3.1 is why step 3 matters: it deployed and passed its health checks, and every
OpenCode job failed at start. Only a real job showed it. A CI smoke test of the job
image (#44) catches some of this before a release.

## Design points

- The deployer is not a Drella job: job pods have no cluster access, by design. It
  has its own service account, with rights in the `drella` namespace only.
- Secrets stay out of git. The Postgres secret becomes a pre-created Secret instead
  of a kustomize generator, so the deployer needs no files on the host.
- Pausing auto-deploy is a setting. A bad release can be pinned back by hand. The
  deploy history is kept.
- Not Flux or Argo CD for now: they'd cover step 2 but not the job smoke test or
  rollback without extra parts, and they want secrets in git (SOPS). Revisit if more
  services move onto the cluster.

## Open questions

- Who may merge the release PR (O2)?
- How long the smoke test may take, and which model it uses if the local one is down.
- Whether to deploy straight away or in a window.
