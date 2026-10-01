#!/bin/bash
set -e

# In rootless Docker, host UID 1000 maps to container UID 0 (root).
# Claude Code refuses to run as root, so we drop to user "dev".
# We use ACLs to ensure both root (=host user) and dev can read/write
# all project files.

# Which agent this run launches (claude, opencode, or codex). Skip the
# Claude-only auth/trust handling without a separate image.
AGENT="${SANDBOX_AGENT:-claude}"

if [ -d /workspace/project ]; then
    # Give dev access to existing host-owned files
    setfacl -R -m u:dev:rwX /workspace/project 2>/dev/null || true
    # Default ACL: new files/dirs automatically grant access to both
    setfacl -R -d -m u:dev:rwX /workspace/project 2>/dev/null || true
    setfacl -R -d -m u:0:rwX /workspace/project 2>/dev/null || true
fi

# Ensure dev owns the .claude config directory (Docker volume starts as root)
chown -R dev:dev /home/dev/.claude 2>/dev/null || true

# opencode's config and state+auth dirs are named volumes too and start root
# owned; dev must own them to read its own auth.json and write opencode.db.
if [ "$AGENT" = "opencode" ]; then
    mkdir -p /home/dev/.config/opencode /home/dev/.local/share/opencode
    chown -R dev:dev /home/dev/.config/opencode /home/dev/.local/share/opencode 2>/dev/null || true
fi

# Same for the uv cache volume
mkdir -p /home/dev/.cache/uv
chown -R dev:dev /home/dev/.cache 2>/dev/null || true

# Same for the uv-managed Python interpreter volume
mkdir -p /home/dev/.local/share/uv/python
chown -R dev:dev /home/dev/.local 2>/dev/null || true

# Same for the Cargo dependency and build cache volumes (--rust image only;
# CARGO_HOME is unset in the others). Only the mount points are chowned, not
# their contents: the volumes start out empty and everything inside them is
# written by dev, so a recursive pass would just re-walk a large cache on
# every start.
if [ -n "${CARGO_HOME:-}" ]; then
    CARGO_DIRS=("$CARGO_HOME" "$CARGO_HOME/registry" "$CARGO_HOME/git"
                "${CARGO_TARGET_DIR:-$CARGO_HOME/target}")
    mkdir -p "${CARGO_DIRS[@]}"
    chown dev:dev "${CARGO_DIRS[@]}" 2>/dev/null || true
fi

# The Playwright browsers dir is a named volume, which masks whatever the
# image has at that path once the volume exists. Sync in any browser revision
# baked into the image (under /opt/playwright-seed) that the volume doesn't
# have yet, so a PLAYWRIGHT_CLI_VERSION bump reaches existing volumes on the
# next start. Runtime installs by playwright-cli still land in the volume and
# persist.
mkdir -p /opt/playwright-browsers
chown dev:dev /opt/playwright-browsers 2>/dev/null || true
if [ -d /opt/playwright-seed ]; then
    for src in /opt/playwright-seed/*; do
        [ -e "$src" ] || continue
        dst="/opt/playwright-browsers/$(basename "$src")"
        if [ ! -e "$dst" ]; then
            # Copy under a temp name and rename, so a start interrupted
            # mid-copy can't leave a half-populated revision that looks installed
            rm -rf "$dst.tmp"
            if cp -a "$src" "$dst.tmp" 2>/dev/null; then
                chown -R dev:dev "$dst.tmp" 2>/dev/null || true
                mv "$dst.tmp" "$dst"
            else
                rm -rf "$dst.tmp"
            fi
        fi
    done
fi
# Remove profiles left by the Playwright MCP older images used.
rm -rf /opt/playwright-browsers/mcp-* 2>/dev/null || true

# Copy host auth files so dev user can use the existing credentials.
if [ "$AGENT" = "codex" ]; then
    export CODEX_HOME=/home/dev/.codex
    mkdir -p "$CODEX_HOME"
    # Overlay host setup on the writable volume before login.
    # Missing host files leave sandbox-created config and sessions intact.
    if [ -d /tmp/host-codex-setup/codex ]; then
        cp -R /tmp/host-codex-setup/codex/. "$CODEX_HOME/"
    fi
    if [ -d /tmp/host-codex-setup/agents ]; then
        mkdir -p /home/dev/.agents
        cp -R /tmp/host-codex-setup/agents/. /home/dev/.agents/
        chown -R dev:dev /home/dev/.agents
    fi
    # Remove API keys retained in config by earlier --api-keys runs too.
    while IFS= read -r -d '' config; do
        python3 /usr/local/lib/claude-sandbox/prepare-auth.py codex-config \
            "$config" "$config.tmp" "${SANDBOX_API_KEYS:-false}"
        mv "$config.tmp" "$config"
    done < <(find "$CODEX_HOME" -maxdepth 1 -type f -name '*.toml' -print0)
    if [ -d "$CODEX_HOME/agents" ]; then
        while IFS= read -r -d '' config; do
            python3 /usr/local/lib/claude-sandbox/prepare-auth.py codex-config \
                "$config" "$config.tmp" "${SANDBOX_API_KEYS:-false}"
            mv "$config.tmp" "$config"
        done < <(find "$CODEX_HOME/agents" -type f -name '*.toml' -print0)
    fi
    chown -R dev:dev "$CODEX_HOME"
    chmod 700 "$CODEX_HOME"
    # Drop the Playwright MCP older images registered in the persisted config.
    if grep -qs 'command = "playwright-mcp"' "$CODEX_HOME/config.toml"; then
        gosu dev env HOME=/home/dev CODEX_HOME="$CODEX_HOME" codex mcp remove playwright >/dev/null 2>&1 || true
    fi
    if [ -s /tmp/host-codex-auth.json ]; then
        cp /tmp/host-codex-auth.json "$CODEX_HOME/auth.json"
    fi
    # A missing host login must not erase a login created in the sandbox.
    # Strip API billing credentials left by a previous --api-keys run.
    if [ -f "$CODEX_HOME/auth.json" ]; then
        python3 /usr/local/lib/claude-sandbox/prepare-auth.py codex \
            "$CODEX_HOME/auth.json" "$CODEX_HOME/auth.json.tmp" "${SANDBOX_API_KEYS:-false}"
        if [ -s "$CODEX_HOME/auth.json.tmp" ]; then
            mv "$CODEX_HOME/auth.json.tmp" "$CODEX_HOME/auth.json"
            chown dev:dev "$CODEX_HOME/auth.json"
            chmod 600 "$CODEX_HOME/auth.json"
        else
            rm -f "$CODEX_HOME/auth.json" "$CODEX_HOME/auth.json.tmp"
        fi
    fi
    if [ "${SANDBOX_API_KEYS:-false}" = true ] && [ -n "${OPENAI_API_KEY:-}" ]; then
        printf '%s' "$OPENAI_API_KEY" | gosu dev env HOME=/home/dev codex \
            -c 'cli_auth_credentials_store="file"' login --with-api-key
    fi
elif [ "$AGENT" = "opencode" ]; then
    # opencode keeps auth.json under ~/.local/share/opencode and opencode.json
    # under ~/.config/opencode; the host copies arrive at these /tmp paths.
    mkdir -p /home/dev/.local/share/opencode /home/dev/.config/opencode
    if [ -f /tmp/host-opencode-auth.json ]; then
        cp /tmp/host-opencode-auth.json /home/dev/.local/share/opencode/auth.json
        chown dev:dev /home/dev/.local/share/opencode/auth.json
        chmod 600 /home/dev/.local/share/opencode/auth.json
    fi
    if [ -f /tmp/host-opencode.json ]; then
        cp /tmp/host-opencode.json /home/dev/.config/opencode/opencode.json
        chown dev:dev /home/dev/.config/opencode/opencode.json
        chmod 600 /home/dev/.config/opencode/opencode.json
    fi
else
    # Claude Code keeps .credentials.json under ~/.claude and .claude.json in
    # the home dir; the host copies arrive at these /tmp paths.
    mkdir -p /home/dev/.claude
    if [ -f /tmp/host-credentials.json ]; then
        cp /tmp/host-credentials.json /home/dev/.claude/.credentials.json
        chown dev:dev /home/dev/.claude/.credentials.json
        chmod 600 /home/dev/.claude/.credentials.json
    fi
    if [ -f /tmp/host-claude.json ]; then
        cp /tmp/host-claude.json /home/dev/.claude.json
        chown dev:dev /home/dev/.claude.json
        chmod 600 /home/dev/.claude.json
    fi
fi

# Drop the Playwright MCP older images registered in opencode's persisted
# config. opencode has no `mcp remove`, and `opencode mcp add` left trailing
# commas, so drop those before parsing.
if [ "$AGENT" = "opencode" ] && grep -qs '"playwright-mcp"' /home/dev/.config/opencode/opencode.json; then
python3 - <<'MCPEOF' || true
import json, os, re

path = "/home/dev/.config/opencode/opencode.json"
with open(path) as f:
    config = json.loads(re.sub(r",(\s*[}\]])", r"\1", f.read()))
server = config.get("mcp", {}).get("playwright", {})
if (server.get("command") or [None])[0] == "playwright-mcp":
    del config["mcp"]["playwright"]
    with open(path + ".tmp", "w") as f:
        json.dump(config, f, indent=2)
    os.replace(path + ".tmp", path)
MCPEOF
chown dev:dev /home/dev/.config/opencode/opencode.json 2>/dev/null || true
chmod 600 /home/dev/.config/opencode/opencode.json 2>/dev/null || true
fi

# Point opencode at an OpenAI-compatible server running on the host (the
# --port flag). The container reaches the host over host networking, so
# 127.0.0.1 *is* the host. Merges a "local" provider into the container's
# opencode.json (preserving the user's forwarded config) and, when the server
# is already up, pre-selects one of its models so 'opencode --auto' starts
# without a model prompt.
if [ "$AGENT" = "opencode" ] && [ -n "${OPENCODE_API_PORT:-}" ]; then
if ! python3 - <<'OPENAIEOF'; then
import json, os, urllib.request

path = "/home/dev/.config/opencode/opencode.json"
port = os.environ.get("OPENCODE_API_PORT", "").strip()
host = os.environ.get("OPENCODE_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
base_url = "http://%s:%s/v1" % (host, port)

try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {}
if not isinstance(cfg, dict):
    cfg = {}

# Best-effort discovery of the models the local server advertises; if it isn't
# up yet, skip and let opencode's own /models picker re-query later.
models = {}
try:
    with urllib.request.urlopen(base_url + "/models", timeout=5) as r:
        payload = json.load(r)
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        for m in data:
            if isinstance(m, dict):
                mid = m.get("id")
                if isinstance(mid, str) and mid:
                    models[mid] = {"name": mid}
except Exception:
    pass

provider = {
    "npm": "@ai-sdk/openai-compatible",
    "name": "OpenAI serve (host)",
    "options": {"baseURL": base_url},
}
if models:
    provider["models"] = models
prov = cfg.get("provider")
if not isinstance(prov, dict):
    prov = {}
    cfg["provider"] = prov
prov["local"] = provider
if models and "model" not in cfg:
    cfg["model"] = "local/" + next(iter(models))

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
os.replace(tmp, path)
OPENAIEOF
    echo "warning: could not point opencode at the host OpenAI server (port $OPENCODE_API_PORT)" >&2
fi
chown dev:dev /home/dev/.config/opencode/opencode.json 2>/dev/null || true
chmod 600 /home/dev/.config/opencode/opencode.json 2>/dev/null || true
fi

# Pre-trust the mounted project. Claude Code records trust per absolute path
# under .claude.json's "projects" key, and the host copy only ever holds host
# paths, so every fresh container otherwise re-prompts for /workspace/project.
# A malformed or empty .claude.json must not brick the sandbox, hence the
# fallback to a minimal document and the non-fatal warning. opencode has no
# per-directory trust dialog, so this whole step is Claude-only.
if [ "$AGENT" = "claude" ]; then
if ! python3 - <<'TRUSTEOF'; then
import json, os

path = "/home/dev/.claude.json"
try:
    with open(path) as f:
        config = json.load(f)
except (OSError, ValueError):
    config = {}
if not isinstance(config, dict):
    config = {}
projects = config.setdefault("projects", {})
if not isinstance(projects, dict):
    projects = config["projects"] = {}
project = projects.setdefault("/workspace/project", {})
if not isinstance(project, dict):
    project = projects["/workspace/project"] = {}
project["hasTrustDialogAccepted"] = True

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(config, f, indent=2)
os.replace(tmp, path)
TRUSTEOF
    echo "warning: could not pre-trust /workspace/project in .claude.json" >&2
fi
chown dev:dev /home/dev/.claude.json 2>/dev/null || true
chmod 600 /home/dev/.claude.json 2>/dev/null || true
fi

# Start the CUDA MPS control daemon, so several processes sharing the GPU run
# their kernels concurrently in one context instead of being time-sliced.
# Only reachable in the --gpu image: nvidia-cuda-mps-control is a driver binary
# the NVIDIA Container Toolkit mounts in (it ships with the "utility" driver
# capability), and /dev/nvidiactl only exists when a GPU is passed through.
# The daemon runs as root here; a root-run control daemon spawns a per-user MPS
# server on demand, so dev connects as a client through the pipe directory —
# hence the world-accessible mode on it. Both directory variables are exported
# so the clients gosu starts below look in the same place.
# Set CLAUDE_SANDBOX_NO_MPS=1 to skip (e.g. to profile with Nsight, or to keep
# one client's fatal fault from taking down the others' shared server).
if [ -z "$CLAUDE_SANDBOX_NO_MPS" ] && [ -e /dev/nvidiactl ] \
   && command -v nvidia-cuda-mps-control >/dev/null 2>&1; then
    export CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps
    export CUDA_MPS_LOG_DIRECTORY=/var/log/nvidia-mps
    mkdir -p "$CUDA_MPS_PIPE_DIRECTORY" "$CUDA_MPS_LOG_DIRECTORY"
    chmod 777 "$CUDA_MPS_PIPE_DIRECTORY"
    nvidia-cuda-mps-control -d >/dev/null 2>&1 || true
fi

# Allow dev to access PulseAudio socket
PULSE_DIR=$(find /run/user -maxdepth 2 -name pulse -type d 2>/dev/null | head -1)
if [ -n "$PULSE_DIR" ]; then
    setfacl -R -m u:dev:rwX "$PULSE_DIR" 2>/dev/null || true
fi

# Route ALSA through PulseAudio so Claude Code's native audio module works
# (it uses ALSA directly, but no sound cards are passed to the container)
cat > /etc/asound.conf << 'ASOUNDEOF'
pcm.!default {
    type pulse
}
ctl.!default {
    type pulse
}
ASOUNDEOF

# Mark the mounted project directory as safe for git (owned by root, run as dev)
gosu dev git config --global --add safe.directory /workspace/project

# Copy host git identity (name, email, default branch) but not credentials
if [ -f /tmp/host-gitconfig ]; then
    git_name=$(git config -f /tmp/host-gitconfig user.name 2>/dev/null || true)
    git_email=$(git config -f /tmp/host-gitconfig user.email 2>/dev/null || true)
    default_branch=$(git config -f /tmp/host-gitconfig init.defaultBranch 2>/dev/null || true)
    if [ -n "$git_name" ]; then
        gosu dev git config --global user.name "$git_name"
    fi
    if [ -n "$git_email" ]; then
        gosu dev git config --global user.email "$git_email"
    fi
    if [ -n "$default_branch" ]; then
        gosu dev git config --global init.defaultBranch "$default_branch"
    fi
fi

exec gosu dev "$@"
