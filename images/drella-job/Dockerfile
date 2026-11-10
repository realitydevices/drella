# The job image [20]: OpenCode, Claude Code [28], git, gh and the job wrapper.
#   docker build -t drella-job images/drella-job
FROM debian:bookworm-slim

ARG OPENCODE_VERSION=1.15.11
# Claude Code's native build, pinned, with the SHA-256 checksums from that release's
# manifest (https://downloads.claude.ai/claude-code-releases/<version>/manifest.json).
ARG CLAUDE_CODE_VERSION=2.1.319
ARG CLAUDE_CODE_SHA256_AMD64=dabafec82ca1319fa43d8be36a10fc61b1d11821cb98ad5df04ad95e199b8eed
ARG CLAUDE_CODE_SHA256_ARM64=a7e687fd1cb054d651c7588223de3786c99d4c682365e3c3ae1e59ce7f3a591f
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq ripgrep \
 && mkdir -p -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# OpenCode, from the same release artifacts its install script uses
# (https://opencode.ai/install), pinned. The x64 build needs AVX2.
RUN arch=$(case "${TARGETARCH:-$(dpkg --print-architecture)}" in amd64) echo x64;; arm64) echo arm64;; esac) \
 && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${arch}.tar.gz" \
    | tar -xz -C /usr/local/bin opencode \
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

RUN useradd --uid 1000 --create-home drella && mkdir -p /work /out && chown drella /work /out
USER 1000
WORKDIR /work
ENTRYPOINT ["drella-job"]
