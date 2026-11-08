# The job image [20]: OpenCode, git, gh and the job wrapper.
#   docker build -t drella-job images/job
FROM debian:bookworm-slim

ARG OPENCODE_VERSION=1.15.11
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

COPY drella-job /usr/local/bin/drella-job

RUN useradd --uid 1000 --create-home drella && mkdir -p /work /out && chown drella /work /out
USER 1000
WORKDIR /work
ENTRYPOINT ["drella-job"]
