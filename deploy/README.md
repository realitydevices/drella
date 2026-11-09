# Getting started: a cluster for Drella

This page takes a Linux host with no cluster to a running Drella and its first token.
It's a page, not a script: run the steps yourself and adapt them to your host.

## Prerequisites

- A Linux host with a few GB of RAM and disk. Each job gets up to 2 GB of RAM, and
  v0 runs one job at a time.
- A model endpoint the host can reach: an OpenAI-compatible API that needs no API
  key, such as Ollama on your LAN. (v0 passes no model credentials to jobs, so hosted
  APIs that need a key don't work yet.) Check it from the host:

  ```sh
  curl http://<ollama-host>:11434/v1/models
  ```

- `kubectl` on your laptop.

## Install k3s

On the host:

```sh
curl -sfL https://get.k3s.io | sh -s - --disable traefik
```

Leave out `--disable traefik` if nothing else on the host uses ports 80/443 and you want
k3s's built-in ingress.

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
your LAN or in the cluster. (Anything with a public address, including the node itself
if it has one, counts as the public internet.) If your model endpoint uses a different port, edit the last
rule in `job-network.yaml`. That policy assumes k3s's default pod and service ranges
(`10.42.0.0/16`, `10.43.0.0/16`); change them there if yours differ.

The Postgres password is set in `deploy/kustomization.yaml`. Only the Drella pod can
reach Postgres, but you can change it before your first deploy.

Then create the first token:

```sh
kubectl -n drella exec deploy/drella -- drella token create --name <your name>
```

It's printed once. Drella keeps only its hash, and the credentials you store are
encrypted with it, so keep it somewhere safe.

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

# 1. Store a GitHub credential (needed only for jobs with a repo): a fine-grained
#    PAT for the target repo only, with Contents and Pull requests read/write, and
#    without the Workflows permission (so it can't edit .github/workflows). Protect
#    the repo's default branch (pull requests only, no force pushes). The agent holds
#    this token and has internet access, so treat it as exposed to whatever the agent
#    reads; and the repo's CI will run code the agent wrote. See Security in
#    docs/v0.md.
curl -X PUT -H "$auth" -H 'Content-Type: application/json' \
  -d '{"value": "github_pat_..."}' $DRELLA/credentials/github

# 2. Create a job. `endpoint` is an OpenAI-compatible base URL that needs no API key;
#    for Ollama, add /v1. `model` is a model the endpoint serves (`ollama list`).
#    Leave out "repo" for a job with no repo: it opens no PR, and its results are
#    whatever the agent writes to /out.
curl -X POST -H "$auth" -H 'Content-Type: application/json' -d '{
  "prompt": "add a CHANGELOG.md",
  "repo": "<owner>/<repo>",
  "model": "<model>",
  "endpoint": "http://<ollama-host>:11434/v1"
}' $DRELLA/jobs

# 3. Follow its logs (add -H 'Accept: text/event-stream' for server-sent events).
curl -N -H "$auth" "$DRELLA/jobs/<id>/logs?follow=1"

# 4. Check the result and download its outputs.
curl -H "$auth" $DRELLA/jobs/<id>
curl -H "$auth" $DRELLA/jobs/<id>/outputs/pr-url.txt
curl -H "$auth" $DRELLA/jobs/<id>/outputs/diff.patch
curl -H "$auth" $DRELLA/jobs/<id>/outputs/transcript.txt
```

A job with a repo succeeds when the agent has opened a pull request; a job without one
succeeds when the agent finishes cleanly. Jobs time out after an hour; the agent is
stopped five minutes before that, so its work so far is uploaded.

**Ollama's context window:** OpenCode's prompts are long. If the model loses track,
raise Ollama's context length on the Ollama host (e.g. `OLLAMA_CONTEXT_LENGTH=32768`).
