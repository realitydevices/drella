# The job image [20]: uv, Python 3.13, OpenCode, Claude Code [28], git, gh and the job wrapper.
#   docker build -t drella-job images/drella-job
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

ARG OPENCODE_VERSION=1.15.11
# SHA-256 of opencode-linux-{x64,arm64}.tar.gz for that version, as published in the
# GitHub release's asset digests.
ARG OPENCODE_SHA256_AMD64=49317253722c698394980e1921ff28e919d79bb29d5c3f4cf314a4adaf7037cd
ARG OPENCODE_SHA256_ARM64=93e4399f308c49387c25ec2b570602bf0f9dd5f57989427946c0c28dbf259ff4
# Fingerprints GitHub publishes for the gh apt repository's signing keys
# (https://github.com/cli/cli/blob/trunk/docs/install_linux.md).
ARG GH_APT_KEY_FPRS="2C6106201985B60E6C7AC87323F3D4EA75716059 7F38BBB59D064DBCB3D84D725612B36462313325"
# Claude Code's native build, pinned, with the SHA-256 checksums from that release's
# manifest (https://downloads.claude.ai/claude-code-releases/<version>/manifest.json).
ARG CLAUDE_CODE_VERSION=2.1.319
ARG CLAUDE_CODE_SHA256_AMD64=dabafec82ca1319fa43d8be36a10fc61b1d11821cb98ad5df04ad95e199b8eed
ARG CLAUDE_CODE_SHA256_ARM64=a7e687fd1cb054d651c7588223de3786c99d4c682365e3c3ae1e59ce7f3a591f
ARG TARGETARCH


RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git gnupg jq ripgrep \
 && mkdir -p -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && fprs=$(gpg --show-keys --with-colons /etc/apt/keyrings/githubcli-archive-keyring.gpg \
      | awk -F: '$1=="pub"{p=1;next} $1=="fpr"&&p{print $10;p=0}') \
 && test -n "$fprs" \
 && for f in $fprs; do \
      case " $GH_APT_KEY_FPRS " in *" $f "*) ;; *) echo "unexpected gh apt key fingerprint: $f" >&2; exit 1;; esac; \
    done \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# uv
COPY --from=ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 /uv /usr/local/bin/uv

# Python 3.13, installed into a shared directory so all users can use it
ENV UV_PYTHON_INSTALL_DIR=/usr/share/python
RUN uv python install 3.13 \
 && ln -sf "$(uv python find 3.13)" /usr/local/bin/python3 \
 && ln -sf /usr/local/bin/python3 /usr/local/bin/python

# OpenCode, from the same release artifacts its install script uses
# (https://opencode.ai/install), pinned. The x64 build needs AVX2.
RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) arch=x64; sum=$OPENCODE_SHA256_AMD64;; \
      arm64) arch=arm64; sum=$OPENCODE_SHA256_ARM64;; \
      *) echo "unsupported architecture" >&2; exit 1;; \
    esac \
 && curl -fsSL -o /tmp/opencode.tar.gz \
      "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${arch}.tar.gz" \
 && echo "$sum  /tmp/opencode.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/opencode.tar.gz -C /usr/local/bin opencode \
 && rm /tmp/opencode.tar.gz \
 && opencode --version

# Claude Code, the same binary its install script (https://claude.ai/install.sh)
# downloads, pinned and checksummed.
RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) arch=x64; sum=$CLAUDE_CODE_SHA256_AMD64;; \
      arm64) arch=arm64; sum=$CLAUDE_CODE_SHA256_ARM64;; \
    esac \
 && curl -fsSL -o /usr/local/bin/claude \
      "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/linux-${arch}/claude" \
 && echo "$sum  /usr/local/bin/claude" | sha256sum -c - \
 && chmod 755 /usr/local/bin/claude \
 && claude --version

# Claude Code: no auto-update, telemetry, error reporting or other non-essential
# traffic, so the only host it talks to is the Anthropic API. And, as a second layer
# behind the wrapper's --setting-sources user (docs/v0.3.md), managed settings that
# turn off hooks: nothing in a repo can make Claude Code run a command by itself.
ENV DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1
COPY managed-settings.json /etc/claude-code/managed-settings.json

COPY drella-job /usr/local/bin/drella-job
COPY usage-opencode.jq usage-claude-code.jq /usr/local/lib/drella-job/

RUN useradd --uid 1000 --create-home drella && mkdir -p /work /out && chown drella /work /out \
 && mkdir -p /home/drella/.cache/uv /home/drella/.local/share/uv \
 && chown -R drella:drella /home/drella/.cache /home/drella/.local
USER 1000
WORKDIR /work
ENV UV_CACHE_DIR=/home/drella/.cache/uv
ENTRYPOINT ["drella-job"]
