# wanda as it runs at home: the daemon, the claude CLI its sessions run on, and
# `mem` with the texts it ships into a vault. A session has Bash, and in here
# Bash reaches this container and what is mounted into it: not the Mac's
# files, not the repo's .env, and no one's ~/.claude.

# `mem`, on the toolchain the lab's builder uses, so the binary sessions drive
# at home and the one the lab measures come from one compiler
FROM rust:1.91.1 AS mem
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY memory memory
# cargo reads the whole workspace, so its other member has to be here too
COPY lab/harness lab/harness
RUN cargo build --release --locked -p memory --bin mem

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.9.11
RUN useradd -m -s /bin/bash wanda

USER wanda
# the venv is not on PATH: a session that could run `wanda slack post` could
# answer twice. `mem` is, as the prompt names it.
ENV HOME=/home/wanda \
    PATH=/home/wanda/.local/bin:/opt/wanda/bin:/usr/local/bin:/usr/bin:/bin \
    DISABLE_AUTOUPDATER=1
# the CLI version the lab's runs record (lab/harness/src/bin/run.rs prints
# `claude --version`); changed when a lab round moves to another. Before the
# code, so an upgrade that changes only the code reuses it.
ARG CLAUDE_VERSION=2.1.268
RUN curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_VERSION" \
 && mkdir -p /home/wanda/.claude/projects

USER root
# What PATH finds as `mem` is the wrapper, which dates a call made by hand and
# then runs the real one, kept off PATH. `mem` looks for its templates beside
# itself, and the product beside the `mem` PATH finds: one set, linked.
COPY docker/mem /opt/wanda/bin/mem
COPY memory/templates /opt/wanda/bin/templates
COPY --from=mem /src/target/release/mem /opt/wanda/libexec/mem
RUN ln -s ../bin/templates /opt/wanda/libexec/templates
# A login shell's PATH comes from /etc/profile, which leaves /opt/wanda/bin
# out, and Claude Code runs a session's commands in one when it has no shell
# snapshot to run them in: `mem` has to be found there too.
RUN printf '%s\n' 'PATH="/opt/wanda/bin:$PATH"' > /etc/profile.d/wanda-mem.sh
# The mount points of the vault's and the run store's named volumes, which
# take their owner when first mounted, and of the home directory, owned by
# the session user, who opens the vault, the snapshots and any restore. Their
# parent stays root's: Claude Code reads CLAUDE.md and .claude/ in every
# directory above a session's working directory, and a file a session left
# there would be held by no snapshot and rewritten by no start.
RUN mkdir -p /srv/wanda/vault /srv/wanda/store /srv/wanda/home \
 && chown wanda:wanda /srv/wanda/vault /srv/wanda/store /srv/wanda/home
COPY pyproject.toml uv.lock /opt/wanda/
COPY wanda /opt/wanda/wanda
COPY prompts /opt/wanda/prompts
COPY skills /opt/wanda/skills
# editable, as on the host: the daemon finds prompts/ and skills/ beside its package
RUN cd /opt/wanda && uv sync --locked --no-dev

USER wanda
WORKDIR /srv/wanda
CMD ["/opt/wanda/.venv/bin/wanda", "run"]
