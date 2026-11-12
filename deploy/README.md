# Getting started: a cluster for Drella

This page takes a Linux host with no cluster to a running Drella and its first token.
It's a page, not a script: run the steps yourself and adapt them to your host.

## Prerequisites

- A Linux host with a few GB of RAM and disk. Each job gets up to 2 GB of RAM, and
  v0 runs one job at a time.
- A model endpoint the host can reach: Ollama on your LAN, a hosted OpenAI-compatible
  API, or Anthropic's API. Models that need an API key are fine: the key is stored as
  a credential. For a local endpoint, check it from the host:

  ```sh
  curl http://<ollama-host>:11434/v1/models
  ```

- `kubectl` on your laptop.

## Install k3s

On the host:

```sh
curl -sfL https://get.k3s.io | sh -s - --disable traefik --secrets-encryption
```

Leave out `--disable traefik` if nothing else on the host uses ports 80/443 and you want
k3s's built-in ingress.

**Secrets encryption:** k3s stores Secrets unencrypted in its datastore by default. A
caller's stored GitHub token (and the model's API key) sit in a Kubernetes Secret from
the moment the job is queued; an App-minted token is added only when the job launches.
Drella's own GitHub App key is in the `drella-github-app` Secret; without encryption, a
copy of the k3s datastore exposes them, which is exactly what encrypting credentials with
the caller's token ([decision 19](../docs/decisions.md)) is meant to prevent.
`--secrets-encryption` enables AES-CBC encryption at rest on first install. For an
existing k3s install that was set up without the flag, see the
[Enable Secrets Encryption on an Existing Cluster](https://docs.k3s.io/cli/secrets-encrypt)
procedure in the k3s docs (requires k3s v1.33.10+k3s1, v1.34.6+k3s1, v1.35.3+k3s1 or
later; upgrade first if your version is older). It needs these steps on the single server:

1. `k3s secrets-encrypt status` shows "Disabled, no configuration file found".
2. `k3s secrets-encrypt enable`.
3. Add `--secrets-encryption` to the k3s server's arguments (or `secrets-encryption: true`
   in `/etc/rancher/k3s/config.yaml`) and restart k3s.
4. `k3s secrets-encrypt status` should show `Encryption Status: Disabled`,
   `Current Rotation Stage: start` and `Server Encryption Hashes: All hashes match`.
   Don't continue otherwise: the k3s docs warn that not following the procedure can leave
   the cluster permanently corrupted.
5. `k3s secrets-encrypt rotate-keys`.
6. Restart k3s again with the same arguments. The server then re-encrypts existing Secrets
   (about 5 per second).
7. Wait until `k3s secrets-encrypt status` shows `Enabled` and `reencrypt_finished`.

k3s keeps the encryption key in `/var/lib/rancher/k3s/server/cred/`, on the same host, so
a copy of the whole server directory (or root on the host) can still read Secrets. Keep
datastore backups separate from `server/cred/`.

## Kubeconfig

Copy `/etc/rancher/k3s/k3s.yaml` from the host to your laptop (it's readable by root
only), and change `127.0.0.1` to the host's address:

```sh
ssh you@host sudo cat /etc/rancher/k3s/k3s.yaml > ~/.kube/drella.yaml
sed -i.bak 's/127.0.0.1/<host address>/' ~/.kube/drella.yaml
export KUBECONFIG=~/.kube/drella.yaml
kubectl get nodes
```

## Deploy

The manifests pin a released image (see **Versions** below), so deploy from a release:
the first is `v0.1.0`. Images on `main` are published too, as `:main` and
`:sha-<commit>`, for testing.

From a checkout of this repo:

```sh
kubectl apply -k deploy/
kubectl -n drella rollout status statefulset/postgres
kubectl -n drella rollout status deployment/drella
```

This creates the `drella` namespace with Postgres, the Drella API, a 10 GB volume for
job logs and outputs, and a ServiceAccount that can create Jobs and Secrets and read pod
logs in that namespace. The images come from GHCR: `ghcr.io/realitydevices/drella` and
`ghcr.io/realitydevices/drella-job`. Both are public, so no pull secret is needed.

**Versions:** `kustomization.yaml` pins the `drella` image to a release tag (e.g.
`v0.1.0`), and Drella launches the `drella-job` image with the same tag. To upgrade,
change `newTag` and apply again.

**Job network:** job pods can reach DNS, the Drella API, the public internet, and port
11434 (Ollama's default) on private networks outside the cluster, and nothing else on
your LAN or in the cluster. (Anything with a public address, including the node
itself if it has one, counts as the public internet.) If your model endpoint uses a
different port, edit the last rule in `job-network.yaml`. That policy assumes k3s's default pod and service ranges
(`10.42.0.0/16`, `10.43.0.0/16`); change them there if yours differ.

The Postgres password is set in `deploy/kustomization.yaml`. Only the Drella pod can
reach Postgres, but you can change it before your first deploy.

Then create the first token:

```sh
kubectl -n drella exec deploy/drella -- drella token create --name <your name> \
  --github-profile builder
```

It's printed once. Drella keeps only its hash, and the credentials you store are
encrypted with it, so keep it somewhere safe. `--github-profile` says what its jobs can
do on GitHub when Drella mints their tokens (see **GitHub profiles** below); leave it
out for a read-only token.

## GitHub tokens from a GitHub App (optional)

A job with a repo needs a GitHub token. Either each caller stores their own `github`
credential (see **Run a job**), or Drella holds a GitHub App's private key and mints a
token for each repo job itself. Installation tokens last an hour, so nobody has to keep
storing fresh ones. A caller's own `github` credential still wins if they store one.

**Rollout order:** merge, then set the branch protection and rulesets listed under
**Approvals and branch protection** below on every repo the App will be installed on,
and only then configure the App key in Drella (step 3). Until the protection is in
place, App tokens can write to those repos' default branches.

1. **Create the App** (GitHub → Settings → Developer settings → GitHub Apps → New).
   It needs no webhook. Repository permissions: **Contents** read and write, **Pull
   requests** read and write, **Issues** read-only (Metadata read-only is automatic).
   Leave out **Workflows**, so jobs can't edit `.github/workflows`. Note its **App
   ID**, and generate a **private key** (a `.pem` file).
   Keep the App private ("Only on this account"), so nobody else can install it.
2. **Install it only on the repos Drella's jobs may change** ("Only select
   repositories"). Drella can write to every repo the App is installed on (see the
   security note in [docs/v0.md](../docs/v0.md#security)). Protect those repos as in
   **Approvals and branch protection** below.
3. **Create the Secret** from the `.pem`:

   ```sh
   kubectl -n drella create secret generic drella-github-app \
     --from-file=private-key.pem=<path to the .pem> \
     --from-literal=app-id=<App ID>
   kubectl -n drella rollout restart deployment/drella
   ```

   Or generate it in your own kustomize overlay, next to the `.pem` (there's an
   example in `kustomization.yaml`). Don't put the key in this checkout.

The deployment (`drella.yaml`) reads both from that Secret: `DRELLA_GITHUB_APP_ID` from
`app-id`, and the key as a file, `DRELLA_GITHUB_APP_KEY_FILE`
(`/etc/drella/github-app/private-key.pem`). The Secret is optional: without it Drella
starts as before, and repo jobs need a `github` credential. With it, Drella checks the
key at startup and refuses to start if it can't read it. The key is read again for
every token, so a rotated key takes effect without a restart (a new App ID needs one).
Neither the key nor any token is stored in the database.

**More Apps.** Drella can hold a second App, for the `superstar` profile below (issue #73).
Each App has a name: the one above is `builder`, and it's the App the `builder` and
`reader` profiles mint from. The second is `superstar`, configured by two variables
per App, `DRELLA_GITHUB_APP_<NAME>_ID` and, if its key isn't at
`/etc/drella/github-app-<name>/private-key.pem`, `DRELLA_GITHUB_APP_<NAME>_KEY_FILE`.
Its App ID comes from the optional `drella-github-app-superstar` Secret, which is the
same shape as `drella-github-app` (`private-key.pem` and `app-id`), and `drella.yaml`
mounts its key at that default path. Create it the same way as the first Secret; it's
optional, and without it a `superstar` job fails at launch saying which App is missing.
Each App is checked at startup and minted from the same way, with the same checks.

For each repo job without a `github` credential, Drella mints a token when it launches
the job (not when it's queued: a queued job can wait longer than an hour). The token is
for the job's repo only, with the permissions of the job's Drella token's GitHub
profile (below). If the App isn't installed on the repo, or minting fails for any other
reason (GitHub down, say), the job fails at launch and says why. Transient failures
(GitHub 5xx or 429, network errors) are retried first, up to 3 attempts in all. The
token is the job's only one: there's no way to ask for another. Instead, the job's
deadline is capped at the token's expiry, so the wrapper has finished everything (the
PR check, auto-merge, the final report) while the token is still valid.

A caller who stored a `github` credential before the App was set up has to delete it
(`DELETE /credentials/github`) to get App tokens: a stored one always wins.

By default the agent commits as "Drella agent" (an installation token can't
read the App's own user), and PRs are opened by `<app name>[bot]`.

### GitHub profiles

Each Drella token has a GitHub profile, which decides what the App tokens minted for its
jobs may do. Profiles are a fixed set in code (`PROFILES` in `drella/github.py`):

| Profile | App | Contents | Pull requests | Issues | Checks, statuses | The job |
|---|---|---|---|---|---|---|
| `builder` | `builder` | write | write | read | | pushes a branch and opens a PR (drella-bot's normal use) |
| `reader` | `builder` | read | read | read | | can't push or open PRs; writes its results (a review, a report) to `/out` and succeeds if it wrote any |
| `superstar` | `superstar` | write | write | write | read | the supervisor: acts on GitHub through `gh` during its run (comments, merges), and succeeds if it wrote its report to `/out`, as a `reader` does. Its token can write, so it isn't read-only and doesn't open a PR |

The `superstar` App needs Contents, Pull requests and Issues write, and Checks and Commit
statuses read (nothing else, and no Workflows). Install it on the repos it may act on.
It can approve and merge PRs, so the rules in **Approvals and branch protection** below
apply to it too.

Only an admin sets a token's profile, with the CLI inside Drella's pod. A caller can't
choose one, and can't ask for permissions per job:

```sh
kubectl -n drella exec deploy/drella -- drella token create --name <name> --github-profile builder
kubectl -n drella exec deploy/drella -- drella token list                    # id, profile, role, name
kubectl -n drella exec deploy/drella -- drella token set-profile <id> reader
```

A token also has a **role** (issue #74): `member` (the default) or `operator`, whose jobs
get a job-scoped API token to queue and read the token's jobs
([docs/v0.3.md](../docs/v0.3.md#operator-tokens)). Like the profile, only the CLI sets it:

```sh
kubectl -n drella exec deploy/drella -- drella token create --name superstar \
  --github-profile superstar --role operator
kubectl -n drella exec deploy/drella -- drella token set-role <id> member
```

`DRELLA_OPERATOR_MAX_CHILD_JOBS` (default 6) caps how many jobs one operator job may queue.

A new token is a `reader` unless you say otherwise. Tokens that existed before profiles
(migration 0005) became `builder`s, so they work as before. The profile is read when a
job's token is minted, so changing it also changes the token's queued jobs. It only
applies to App tokens: a caller's own `github` credential can do whatever it can do.

A profile only limits the tokens Drella mints from the App. If a Drella token stores its
own `github` credential, that credential takes precedence: its jobs get whatever that
credential can do on GitHub, and the profile doesn't apply.

To add a profile (say a `reviewer` that comments on PRs), add it to `PROFILES`, with the
App it mints from. Anything with Pull requests write can also approve PRs; see below for
why that approval mustn't count.

### Approvals and branch protection

GitHub permissions can't separate opening a PR from approving one: both are Pull
requests write. So a `builder` job's token (or a prompt-injected agent holding it) can
submit an approving review. What stops that mattering:

- GitHub never lets a PR's author approve it. Every drella-bot PR is authored by
  `drella-bot[bot]`, so drella-bot can't approve its own PRs, including other jobs'.
- It **can** approve a PR someone else opened (e.g. one of yours), and an App's
  approval may count towards "required approvals". So require a **code owner's**
  review: `.github/CODEOWNERS` names `@realitydevices` for every file, and an App can't
  be a code owner. A PR that changes CODEOWNERS needs the current code owner's review
  too.
- Contents write can also push to any branch that isn't protected (including your own
  open PR branches), and push tags. Pushing a `v*` tag publishes release images
  (`images.yml`).

Settings for each repo the App is installed on (GitHub → the repo → Settings). For
this repo, merge the PR that adds `.github/CODEOWNERS` first: it only takes effect
from the default branch.

1. **Branches → branch protection rule for `main`** (or a ruleset targeting the default
   branch):
   - Require a pull request before merging: on.
   - Required approvals: 1.
   - Dismiss stale pull request approvals when new commits are pushed: on.
   - **Require review from Code Owners: on** (new).
   - Require approval of the most recent reviewable push: on (optional; it stops a
     push after your approval being merged on it, which the previous setting already
     covers, and it stops you approving a PR you last pushed to).
   - **Require status checks to pass: on, with `pytest`** (new; today nothing is
     required, so an approved PR auto-merges even with CI failing).
   - Allow force pushes, allow deletions: off.
   - Don't add drella-bot to any bypass list ("Allow specified actors to bypass").
     Whether you enforce the rule for admins is up to you: if you don't, you can merge
     your own PRs (which nobody else can approve) with the admin override, as now.
2. **Rules → Rulesets → New branch ruleset** "Only drella/ branches for the bot":
   target all branches except `main` and `drella/**`; rules: Restrict creations,
   Restrict updates, Restrict deletions, Block force pushes; bypass list: Repository
   admin. Then the bot can only push `drella/*` branches (which is where the wrapper
   tells the agent to work), and can't rewrite your PR branches.
3. **Rules → Rulesets → New tag ruleset** "Release tags": target `v*`; rules: Restrict
   creations, Restrict updates, Restrict deletions; bypass list: Repository admin. Then
   a job can't publish a release image.
4. **General → Pull Requests → Allow auto-merge: on** (already on here). The wrapper
   turns on auto-merge (`gh pr merge --auto --squash`) for every PR a job opens. With
   the rules above, auto-merge only queues the merge: GitHub merges once a code owner
   has approved and `pytest` has passed, and a new push dismisses the approval. Without
   a required review, auto-merge has nothing to wait for, and a job's PR can merge with
   no human looking at it.

## Reaching Drella

Drella exposes one HTTP service, `drella` on port 8000. What sits in front of it is up
to you.

**Port-forward** (simplest):

```sh
kubectl -n drella port-forward svc/drella 8000:8000
curl localhost:8000/healthz
```

**Or a reverse proxy.** For example, with k3s's own ingress (if you kept Traefik), or with
Caddy on the host, pointing at the service's cluster IP:

```sh
kubectl -n drella get svc drella -o jsonpath='{.spec.clusterIP}'
```

```caddyfile
drella.example.com {
    reverse_proxy <cluster IP>:8000
}
```

Use TLS for anything beyond your LAN: every request carries a token.

## Run a job

With `DRELLA=http://localhost:8000` and `TOKEN` set to your token:

```sh
auth="Authorization: Bearer $TOKEN"

# 1. Store a GitHub credential (needed only for jobs with a repo, and not at all if
#    Drella has a GitHub App; see above. A stored one wins over the App: remove it
#    with DELETE $DRELLA/credentials/github): a fine-grained
#    PAT for the target repo only, with Contents and Pull requests read/write, and
#    without the Workflows permission (so it can't edit .github/workflows). Protect
#    the repo's default branch for everyone, admins included: pull requests only,
#    at least one approval, no force pushes. Protect release tags too. The agent
#    holds this token and has internet access, so treat it as exposed to whatever
#    the agent reads; and the repo's CI will run code the agent wrote. See
#    Security in docs/v0.md.
curl -X PUT -H "$auth" -H 'Content-Type: application/json' \
  -d '{"value": "github_pat_..."}' $DRELLA/credentials/github

# 2. Register the models your jobs can use (docs/v0.2.md). Jobs name them by alias.
#    Models are per token: each token registers its own, and sees and uses only those.
#    A local Ollama model: an OpenAI-compatible API with no key. Add /v1 to the
#    base URL; `model_id` is what `ollama list` shows.
curl -X PUT -H "$auth" -H 'Content-Type: application/json' -d '{
  "interface": "openai-compatible",
  "base_url": "http://<ollama-host>:11434/v1",
  "model_id": "<ollama model>",
  "context_length": 65536,
  "max_output_tokens": 8192
}' $DRELLA/models/local

#    A hosted model that needs an API key: store the key as a credential, and name
#    the credential in the model. Drella sends your key only to a base URL you set, but
#    the agent and anything it runs can read it (see Security in docs/v0.2.md). A model
#    with a key needs an https:// base URL. Use a key you can revoke, with a spending
#    limit: the agent can read it.
curl -X PUT -H "$auth" -H 'Content-Type: application/json' \
  -d '{"value": "<anthropic api key>"}' $DRELLA/credentials/anthropic-key
curl -X PUT -H "$auth" -H 'Content-Type: application/json' -d '{
  "interface": "anthropic",
  "base_url": "https://api.anthropic.com/v1",
  "model_id": "<anthropic model id>",
  "credential": "anthropic-key"
}' $DRELLA/models/claude

#    Other hosted APIs (e.g. OpenRouter) use "openai-compatible" with their base URL
#    and a credential.
#
#    Claude Code with your Claude subscription (docs/v0.3.md): store the OAuth token
#    from `claude setup-token` as a credential. The model has no base_url; `model_id`
#    is what `claude --model` takes (e.g. sonnet, opus). Jobs count against your
#    plan's usage limits. Only your own token, for your own jobs: if you host Drella
#    for other people, they use API keys ("anthropic"). See Terms in docs/v0.3.md.
curl -X PUT -H "$auth" -H 'Content-Type: application/json' \
  -d '{"value": "<oauth token>"}' $DRELLA/credentials/claude-oauth
curl -X PUT -H "$auth" -H 'Content-Type: application/json' -d '{
  "interface": "claude-code",
  "model_id": "sonnet",
  "credential": "claude-oauth"
}' $DRELLA/models/claude-code
#    `GET /models` lists your models.

# 3. Create a job, naming a model by alias. Leave out "repo" for a job with no
#    repo: it opens no PR, and its results are whatever the agent writes to /out.
curl -X POST -H "$auth" -H 'Content-Type: application/json' -d '{
  "prompt": "add a CHANGELOG.md",
  "repo": "<owner>/<repo>",
  "model": "local"
}' $DRELLA/jobs

# 4. Follow its logs (add -H 'Accept: text/event-stream' for server-sent events).
curl -N -H "$auth" "$DRELLA/jobs/<id>/logs?follow=1"

# 5. Check the result and download its outputs.
curl -H "$auth" $DRELLA/jobs/<id>
curl -H "$auth" $DRELLA/jobs/<id>/outputs/pr-url.txt
curl -H "$auth" $DRELLA/jobs/<id>/outputs/diff.patch
curl -H "$auth" $DRELLA/jobs/<id>/outputs/transcript.txt
```

A job with a repo succeeds when the agent has opened a pull request; a job without one
succeeds when the agent finishes cleanly and has written at least one output. Jobs time
out after an hour; the agent is stopped five minutes before that, so its work so far is
uploaded.

**Ollama's context window:** OpenCode's prompts are long. If the model loses track,
raise Ollama's context length on the Ollama host (e.g. `OLLAMA_CONTEXT_LENGTH=65536`),
and set the model's `context_length` to match. OpenCode takes a model's limits only as a
pair, so Drella passes them only when both `context_length` and `max_output_tokens` are
set.
