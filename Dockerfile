FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source. A .dockerignore keeps secrets (.env), the ~96MB
# genius.db, .git history, and generated pipeline output out of the build
# context — do not remove it.
COPY . .

# ---------------------------------------------------------------------------
# Agent provider CLIs (agy / claude / codex / grok / nlm)
# ---------------------------------------------------------------------------
# This image intentionally ships WITHOUT the agent CLIs. It is enough to run
# the coordination surfaces — the distributed hub (`serve.py --distributed`),
# the dashboard, and the per-role API servers — but any call that actually
# invokes an agent shells out to one of these local CLIs and will fail if it
# is absent. They are deliberately left out because each needs interactive,
# per-user authentication (e.g. `grok login`, `nlm login`, or provider API
# keys) that cannot and must not be baked into an image.
#
# To run real pipelines in a container, pick one:
#   1. Extend this image with the CLIs you use and mount your authenticated
#      config at runtime, e.g. `-v $HOME/.config/genius-clis:/root/.config`.
#   2. Mount the host binaries + their auth config into the container.
#   3. Run the pipeline on the host and use the container only for the hub /
#      dashboard / distributed workers.
#
# Optional opt-in layer for the npm-published CLIs (uncomment to bake them in;
# they still require runtime auth via mounted config or API-key env vars):
#
#   RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
#       && npm install -g @anthropic-ai/claude-code @openai/codex \
#       && rm -rf /var/lib/apt/lists/*
#
# (agy and nlm are not on public npm; install them per their own docs.)
# ---------------------------------------------------------------------------

# Expose ports: Hub (8000), Researcher (8001), Claude (8002), Codex (8003), Tester (8004), Security (8005), DevOps (8006), Dashboard (8080)
EXPOSE 8000 8001 8002 8003 8004 8005 8006 8080

# Default command to run serve.py. docker-compose overrides `command:` per
# service; run with an explicit role/mode in production (the bare menu is
# interactive and expects a TTY).
CMD ["python", "serve.py"]
