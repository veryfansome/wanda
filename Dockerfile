# The lab, in a box. Nothing from the host's ~/.claude gets in: no global
# CLAUDE.md, no settings.json, no plugins, no plugin skills. Every wanda
# session before this ran with the owner's personal instruction framework in
# its context and the owner's plugin skills invocable, because Claude Code
# resolves those from the home directory and `git init` in the vault could only
# stop the project-level inheritance. Here the home directory is empty.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# unprivileged, so files written to the mounted repo are not root's
RUN useradd -m -s /bin/bash lab
USER lab
ENV HOME=/home/lab \
    PATH=/home/lab/.local/bin:$PATH

# the same installer the host used; pinned by the lab, not by the base image
RUN curl -fsSL https://claude.ai/install.sh | bash

WORKDIR /work

# the repo is mounted from the host and owned by a different uid, which git
# refuses to touch; without this the harness cannot read its own revision
RUN git config --global --add safe.directory /work
