# Job links

Status: agreed in principle (2026-11-10), names not final.

Jobs link to each other, so Drella becomes a work tracker and the links become the
audit trail (code → review → fix → merge).

## Link types

- **part of** (structure): a job belongs to a production, which can be part of a
  bigger one, which gives recursion. A planner's children are the jobs it cut.
- **waits for** (scheduling): B starts only when A finishes, on success, on failure
  or always. The only link the queue acts on; it must stay acyclic.
- **built on** (provenance): B used A's output. Audit only. (PROV-style names such as
  "derived from" are the alternative.)
- **related to:** free-form.

## Design points

- The graph grows while the work runs: jobs (with their scoped token) start
  follow-on jobs, and Drella records the links. "Code → review → fix → merge" is a
  template, not a fixed pipeline.
- Data model: a `job_links` table (from, to, type, condition). A production is a job
  with children.
