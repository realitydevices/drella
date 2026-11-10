# The build loop

Status: agreed in principle (2026-11-10), not specced.

Drella builds itself: build → review → fix → approve and merge → deploy → find the
next step → repeat.

## What we've learned so far

From the first runs on 2026-11-10:
- a local Qwen (35B-A3B) is a workable builder for small, tight chunks, especially
  with specific review feedback; it stalls on noisy output (long CI logs);
- local Gemma couldn't drive tools reliably;
- Claude (Claude Code via Drella) is a strong reviewer: it checks claims against the
  repo and gives exact fixes;
- tests are the gate, so the job image needs the project's toolchain;
- security-sensitive chunks need adversarial review until a round finds nothing new.

## Pieces

- **Roles:** planner (cuts the next chunks as issues, each with a "done when"),
  builder, reviewer (a different, stronger model than the builder), fixer.
- **Chunk size:** start with one PR, one testable behaviour; the planner splits a
  chunk that times out or is rejected twice, and merges ones that come out trivial.
- **Outcomes:** record size, model, review rounds, merged or reverted, and cost for
  every job, so the planner can learn what works.
- **Autonomy as settings:** who may merge, chunk size, paths an agent may touch,
  concurrency. Raise them as the outcome data justifies it.
- **Fix jobs on an existing PR:** the wrapper currently expects every repo job to
  open a new PR, and reports a fix pushed to an existing branch as failed.
- **The GitHub token minted inside Drella** from the GitHub App, per job, instead of
  by hand.
- **Auto-merge** when approved and CI passes (#42).
- **Deploys:** see [auto-deploy.md](auto-deploy.md).
