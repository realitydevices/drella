# The job image: uv, Python 3.13, OpenCode, git, gh and the job wrapper.
#   docker build -t drella-job images/drella-job
FROM debian:bookworm-slim

ARG OPENCODE_VERSION=1.15.11
ARG TARGETARCH
ARG UV_VERSION=0.8.22

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

# uv
COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION} /uv /usr/local/bin/uv

# Python 3.13, installed into a shared directory so all users can use it
ENV UV_PYTHON_INSTALL_DIR=/usr/share/python
RUN uv python install 3.13 \
 && PYTHONDIR=$(find /usr/share/python -mindepth 1 -maxdepth 1 -type d -name '3.13-*' | head -1) \
 && ln -sf "$PYTHONDIR/python3" /usr/local/bin/python3 \
 && ln -sf /usr/local/bin/python3 /usr/local/bin/python

# OpenCode, from the same release artifacts its install script uses
# (https://opencode.ai/install), pinned. The x64 build needs AVX2.
RUN arch=$(case "${TARGETARCH:-$(dpkg --print-architecture)}" in amd64) echo x64;; arm64) echo arm64;; esac) \
 && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${arch}.tar.gz" \
    | tar -xz -C /usr/local/bin opencode \
 && opencode --version

COPY drella-job /usr/local/bin/drella-job

RUN useradd --uid 1000 --create-home drella && mkdir -p /work /out && chown drella /work /out \
 && mkdir -p /home/drella/.cache/uv /home/drella/.local/share/uv \
 && chown -R drella /home/drella/.cache/uv /home/drella/.local/share/uv
USER 1000
WORKDIR /work
ENV UV_CACHE_DIR=/home/drella/.cache/uv
ENTRYPOINT ["drella-job"]
