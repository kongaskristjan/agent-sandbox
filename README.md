# agent-sandbox

A Docker-based sandbox for running Claude Code, OpenCode, or Codex with automatic approvals inside the container. Images are built from machine-local **profiles** that combine a base (Ubuntu or CUDA), **features** (Python, Rust, TypeScript, Playwright, or your own), and the agents you use.

## What it does

- Runs Claude Code, OpenCode, or Codex inside a Docker container
- Builds one image per profile; a configuration dialog creates the first profile on first run
- Bind-mounts your project directory so you can edit files from both host (VS Code) and container (Claude Code)
- Forwards your selected agent's subscription login (including Codex ChatGPT login); API keys require `--api-keys`
- Shares your personal skills (`~/.claude/skills`), so they work inside and outside the sandbox
- Enables voice mode (`/voice`) via PulseAudio/PipeWire passthrough
- Auto-rebuilds the container image on each run
- Auto-detects rootless vs rootful Docker and applies appropriate security settings

## Prerequisites

- Linux with Docker installed (rootless or rootful), or macOS with Docker Desktop
- NVIDIA GPU with [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) (only for profiles with `base = "cuda"`)
- Python 3 on the host (with `tomli` installed on Python older than 3.11 for reading the profile config and Codex TOML configuration)
- Your chosen agent logged in on the host, or an API key with explicit `--api-keys` opt-in
- PulseAudio or PipeWire (for voice mode)

On Python older than 3.11, install the TOML parser with
`python3 -m pip install tomli`. Python 3.11+ uses the built-in `tomllib` module
and needs no additional package.

## Setup

```bash
git clone <this-repo> ~/agent-sandbox
chmod +x ~/agent-sandbox/agent-sandbox
```

Optionally add to your PATH:

```bash
ln -s ~/agent-sandbox/agent-sandbox ~/.local/bin/agent-sandbox
```

The first run in a terminal asks a few questions to create a profile, then
builds its image (`agent-sandbox-<profile>`). A `cuda` base is a ~10GB
download on first build.

The Compose project name is pinned to `agent-sandbox` regardless of the clone
directory or profile, so volumes are named `agent-sandbox_*` and shared by every
profile that mounts them. Volumes from the project's former name
(`claude-sandbox_*`) are not reused.

## Usage

```bash
# Run the default profile's default agent in the current directory
~/agent-sandbox/agent-sandbox

# Run in a specific project
~/agent-sandbox/agent-sandbox ~/projects/my-dl-project

# Use another profile
~/agent-sandbox/agent-sandbox -p gpu ~/projects/my-dl-project

# Run opencode or Codex instead of the profile's default agent
~/agent-sandbox/agent-sandbox --opencode ~/projects/my-project
~/agent-sandbox/agent-sandbox --codex ~/projects/my-project

# Explicitly opt in to API-key forwarding
~/agent-sandbox/agent-sandbox --codex --api-keys ~/projects/my-project

# Point opencode at an OpenAI-compatible server on the host (e.g. a local
# 'openai serve' on port 8080) instead of a hosted provider
~/agent-sandbox/agent-sandbox --opencode --port 8080 ~/projects/my-project

# Enable host networking (rootful Docker only — always on for rootless)
~/agent-sandbox/agent-sandbox --host-network ~/projects/my-project

# Add, edit or delete profiles, or list them
~/agent-sandbox/agent-sandbox config
~/agent-sandbox/agent-sandbox config --list
```

The wrapper auto-detects whether Docker is rootless or rootful and prints the detected mode and profile at startup. Use `--host-network` when you need the container to access services on localhost (e.g., a dev server on port 8080).

Claude Code starts with `--dangerously-skip-permissions`, opencode with `--auto`, and Codex with `--dangerously-bypass-approvals-and-sandbox` inside the container. Docker provides the isolation boundary. Your project files are mounted at `/workspace/project`.

### Profiles

Profiles live in `~/.config/agent-sandbox/config.toml` (or under
`$XDG_CONFIG_HOME`). `agent-sandbox config` edits them interactively (and
rewrites the file, dropping comments); editing the file by hand works too:

```toml
default = "dev"

[profiles.dev]
base = "ubuntu"                 # or "cuda" (NVIDIA GPU passthrough)
features = ["python", "rust", "node-ts", "playwright"]
agents = ["claude", "opencode", "codex"]  # installed agents, at least one
default_agent = "claude"        # runs when no agent flag is given
network = "bridge"              # or "host"
ports = [3000, "5173:5173"]     # published ports; 3000 means 3000:3000
```

Every key except the table name is optional; the defaults are `ubuntu`, no
features, all agents, the first agent, `bridge` and no ports.

The profile for a run is, in order: `-p/--profile <name>`, the `profile` key
of `<project-dir>/.agent-sandbox.toml`, then `default`. That per-project file
can only hold small overrides:

```toml
profile = "gpu"
ports = [8080]      # added to the profile's ports
network = "host"    # replaces the profile's network
```

`--opencode`, `--codex` and `--agents` pick an agent at run time; asking for an
agent the profile doesn't install is an error.

Ports are published with `docker compose run --service-ports`. A port like
`3000` binds on all host interfaces; write `"127.0.0.1:3000:3000"` to keep it
local. Under host networking (rootless Docker, `network = "host"`,
`--host-network` or `--port`) the container already shares the host's ports,
so configured ports are skipped with a warning.

Each run regenerates the profile's build context in
`~/.cache/agent-sandbox/profiles/<name>/` (or under `$XDG_CACHE_HOME`): a
Dockerfile, a Compose override and copies of the install scripts. The
Dockerfile runs, in order: the base image, one apt layer with every feature's
packages, Node.js, the features (dependencies first, otherwise by name, so
profiles share cached layers), then the agents.

### Features

A feature is a directory with an `install.sh` (run as root at build time) and
an optional `feature.toml`:

```toml
description = "Shown in the configuration dialog"
requires = ["python"]                      # pulled in and installed first
apt = ["libpq-dev"]                        # merged into the shared apt layer
volumes = { "my-cache" = "/home/dev/.cache/my-tool" }  # named volumes
compose = { cap_add = ["SYS_PTRACE"] }     # extra Compose service keys

[env]   # ENV before the install step, so the script and the image see it
MY_TOOL_HOME = "/opt/my-tool"
```

Built-in features are in `features/`:

| Feature | Installs | Volumes |
| --- | --- | --- |
| `python` | Python 3, pip, venv, uv (`UV_PROJECT_ENVIRONMENT=.venv-container`) | `pip-cache`, `uv-cache`, `uv-python` |
| `rust` | rustup stable, rustfmt, clippy, `pkg-config`, `libssl-dev` | `cargo-registry`, `cargo-git`, `cargo-target` |
| `node-ts` | TypeScript, tsx, pnpm (global npm packages) | — |
| `playwright` | Playwright CLI + headless Chromium and its OS deps | `playwright-browsers` |

Add your own in `~/.config/agent-sandbox/features/<name>/`; one named like a
built-in replaces it. The whole directory except `feature.toml` is copied into
the build, so `install.sh` can use files next to it. Volumes are named
`agent-sandbox_<volume>` and are root-owned unless the mount point exists in
the image: create and `chown dev:dev` it in `install.sh` so Docker copies the
ownership into a new volume.

### Authentication

Subscription/OAuth logins transfer by default. **API keys do not**: exporting
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` alone does not forward them. Pass
`--api-keys` to forward those variables and stored API-key credentials. This
opt-in applies to all three agents, including Claude's legacy `primaryApiKey`.

The wrapper stages only the selected agent's auth/config in private temporary
storage (auth and config files use `0600`) and mounts those copies read-only.
It filters API-key auth entries and API-key fields in JSON and Codex TOML config
before Docker can access them, then removes the temporary files on exit.
Host auth/config files are not modified.
Use the wrapper rather than invoking Compose directly so this staging happens.

**Claude Code** (default): Run `claude` on the host and complete the subscription
login. On macOS, the wrapper exports the `Claude Code-credentials` login Keychain
item to its private temporary directory; expect a Keychain prompt on first use.
With `--api-keys`, `ANTHROPIC_API_KEY` can be used instead.

**Codex** (`--codex`): The wrapper forwards the ChatGPT login tokens from
`$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). It excludes the
`OPENAI_API_KEY` field by default, including when the file also contains OAuth
tokens. Codex runs with file-based credential storage and a trusted
`/workspace/project` directory. Its writable auth, sessions, and config persist
in the `codex-config` volume at `/home/dev/.codex`. A host login is copied on
startup; if none is available, an existing sandbox login is retained. Token
refreshes inside the sandbox stay in that volume and are not written back to
the host.

Each `--codex` launch also copies setup from `$CODEX_HOME` (default `~/.codex`):
top-level TOML files (including `config.toml` and named profiles), `AGENTS.md`,
`AGENTS.override.md`, `instructions.md`, and the `agents`, `skills`, `rules`, and
`prompts` directories. Personal skills from `~/.agents/skills` are copied to
the same location under the container user's home. Skill symlinks are
dereferenced so their contents are available inside the sandbox.

Host setup overwrites matching sandbox files on each launch; files that exist
only in the sandbox are retained. Host sessions, history, logs, and caches are
not copied. The copies are owned by the container user, and changes stay inside
the sandbox. API-key fields and literal provider `experimental_bearer_token`
values in configuration require `--api-keys`; they are also stripped from
retained sandbox configuration on runs without that option. Custom MCP
executables and absolute paths in copied configuration must be available inside
the container.

If your host login is stored only in an OS keyring, create a file-based login
first (see [Codex authentication](https://learn.chatgpt.com/docs/auth)):

```bash
codex -c 'cli_auth_credentials_store="file"' login
agent-sandbox --codex
```

A missing Codex login is not fatal: you can also log in inside the container.
With `--api-keys`, a forwarded `OPENAI_API_KEY` takes precedence over the copied
ChatGPT login.

**opencode** (`--opencode`): OAuth entries from
`~/.local/share/opencode/auth.json` are forwarded. API-key entries require
`--api-keys`, as do `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. Its JSON config at
`~/.config/opencode/opencode.json` is forwarded with API-key fields removed by
default. State persists in the `opencode-data` volume. A missing host login is
not fatal; opencode can log in interactively inside the container.

**`--port <port>`** points opencode at an OpenAI-compatible server running on the
host (for example a local `openai serve`) instead of a hosted provider. The
wrapper auto-enables host networking so the container can reach the host, and the
entrypoint merges a `local` provider into opencode's config targeting
`http://127.0.0.1:<port>/v1`, auto-discovering the server's models from
`/v1/models` and pre-selecting the first one so `--auto` starts without a prompt
(a user-set `model` in the forwarded config is left alone). It's opencode-only:
passing `--port` without `--opencode` is an error. Override the target host with
`OPENCODE_API_HOST` (default `127.0.0.1`, correct under host networking).

### Voice mode

Voice mode (`/voice`) works out of the box. The container routes audio through your host's PulseAudio/PipeWire.

### GPU sharing (CUDA MPS)

With a `cuda` profile, the entrypoint starts the CUDA MPS control daemon
(`nvidia-cuda-mps-control -d`) so that several processes hitting the same GPU
run their kernels concurrently in one context instead of being time-sliced —
useful when Claude Code runs training, evaluation, and a notebook side by side.
The pipe and log directories are `/tmp/nvidia-mps` and `/var/log/nvidia-mps`
(exported as `CUDA_MPS_PIPE_DIRECTORY` / `CUDA_MPS_LOG_DIRECTORY`).

Set `AGENT_SANDBOX_NO_MPS=1` to skip it — MPS clients share one server process,
so a fatal fault in one can take down the others, and Nsight profiling of an MPS
client is restricted. Ubuntu-based images ignore the variable; the daemon only
starts when a GPU is actually passed through.

### Rust feature

The `rust` feature installs a rustup-managed stable toolchain (plus `rustfmt`
and `clippy`). Common `-sys` crate build inputs (`pkg-config`, `libssl-dev`,
`cmake`, `build-essential`) are present.

Cargo's caches are persistent named volumes, the same way the uv cache is, so
dependencies are downloaded and compiled once instead of once per container:

| Volume | Path | Contents |
| --- | --- | --- |
| `cargo-registry` | `$CARGO_HOME/registry` | downloaded and unpacked crates |
| `cargo-git` | `$CARGO_HOME/git` | git dependency checkouts |
| `cargo-target` | `$CARGO_HOME/target` | build artifacts (`CARGO_TARGET_DIR`) |

Two consequences of `CARGO_TARGET_DIR=/home/dev/.cargo/target`:

- Builds inside the sandbox do **not** write a `target/` into your project
  directory, so they can't collide with a `target/` the host built with a
  different toolchain (the same reasoning behind `UV_PROJECT_ENVIRONMENT=.venv-container`
  in the `python` feature). Artifacts live at `/home/dev/.cargo/target/<profile>/`
  inside the container; `cargo run` and `cargo test` find them as usual. Run
  `CARGO_TARGET_DIR=target cargo build` for the rare case where you want the
  output in the project directory instead.
- All projects share one target directory, so cargo reuses dependency builds
  whose fingerprint (version, features, profile, compiler) matches across
  projects. `docker volume rm agent-sandbox_cargo-target` clears it if it grows
  too large.

The toolchain itself lives in `$RUSTUP_HOME` (`/home/dev/.rustup`) and
`$CARGO_HOME/bin` — image content, not volumes, so a rebuild always wins over
what an old volume holds. `rustup update` and `cargo install` work inside a
session but, like anything outside the cache volumes, don't survive it.

### Playwright feature (browser automation)

With the `playwright` feature, [Playwright CLI](https://github.com/microsoft/playwright-cli)
(`playwright-cli`) and a headless Chromium are pre-installed, so the agent can drive a browser via
shell commands (`playwright-cli open <url>`, `snapshot`, `click e15`, …) with no
setup. No skill or MCP server is registered for it; to have an agent use it,
mention it in your `AGENTS.md`/`CLAUDE.md` (`playwright-cli --help` documents
every command).

- Only `chrome-headless-shell` is installed; full Chrome-for-Testing crashes
  under the container's security profile and there is no display, so
  `--headed` and `--browser=chrome` don't work. `~/.playwright/cli.config.json`
  sets `"browserName": "chromium"` so the CLI uses the shell instead of the
  absent Chrome channel; a project's own `.playwright/cli.config.json` takes
  precedence.
- Output (snapshots, screenshots) goes to `.playwright-cli/` in the current
  directory; add it to the project's `.gitignore`.
- `@playwright/cli` bundles its own playwright-core, so `PLAYWRIGHT_CLI_VERSION`
  is the single pin for the CLI, its OS deps and the browser.
- `/opt/playwright-browsers` is a persistent named volume, so the browser is
  baked into `/opt/playwright-seed` and the entrypoint copies missing revisions
  in at start. To upgrade, bump `PLAYWRIGHT_CLI_VERSION` in
  `features/playwright/feature.toml`; the next run rebuilds.
- Earlier images shipped the Playwright MCP server instead. The entrypoint
  removes its stale registration from the Codex and opencode config volumes.
  Old browser revisions stay in the volume; run
  `docker volume rm agent-sandbox_playwright-browsers` to reclaim the space.

### Personal skills

Your host skills directory (`~/.claude/skills`) is bind-mounted read-only at
`/home/dev/.claude/skills`, so a skill you install once on the host is available
both when you run `claude` normally and when you run it in the sandbox. Drop a
`~/.claude/skills/<name>/SKILL.md` on the host and the next container start picks
it up — nothing to rebuild.

The container's `~/.claude` is a persistent named volume, which would otherwise
mask the host's skills; the mount targets a path *inside* that volume, and Docker
orders mounts by path depth, so the deeper bind wins. It's read-only: skills are
inputs, and the container has no business writing to the host's config.

### Editing files

Files are bind-mounted, so you can:
- Edit in VS Code on the host while Claude Code works inside the container
- Run `git push` from the host (no git credentials are passed to the container)
- Changes from either side are immediately visible to the other

### Options

```
Usage: agent-sandbox [-p <profile>] [--host-network] [--update] [--agents|--opencode|--codex] [--api-keys] [--port <port>] [project-dir]
       agent-sandbox config [--list]

Commands:
  config          Add, edit or delete profiles and pick the default
  config --list   List profiles

Options:
  -p, --profile <name>  Use this profile (default: 'profile' in the
                  project's .agent-sandbox.toml, else the config default)
  --host-network  Use host networking (always enabled for rootless Docker,
                  opt-in for rootful Docker)
  --update        Refresh the agent binaries in the image (other layers
                  stay cached); future runs reuse the refreshed layer
  --agents        Launch the background-agents view ('claude agents')
                  instead of an interactive session
  --opencode      Run opencode instead of the profile's default agent
  --codex         Run Codex using your host ChatGPT login and setup
                  (without these, the profile's default_agent runs)
  --api-keys      Also forward stored API keys and ANTHROPIC_API_KEY /
                  OPENAI_API_KEY (disabled by default)
  --port <port>   (opencode only) Point opencode at an OpenAI-compatible
                  server running on the host at this port; auto-enables
                  host networking and auto-discovers its models

Environment variables:
  AGENT_SANDBOX_MODE=rootless|rootful   Override Docker mode auto-detection
  AGENT_SANDBOX_NO_MPS=1                Don't start the CUDA MPS control daemon
  ANTHROPIC_API_KEY=sk-ant-...          API key (requires --api-keys)
  OPENAI_API_KEY=sk-...                API key (requires --api-keys)
  CODEX_HOME                          Host Codex directory (default ~/.codex)
  OPENCODE_API_HOST=127.0.0.1          Host address --port targets (opencode)
  XDG_CONFIG_HOME, XDG_CACHE_HOME     Locations of config.toml and generated files
```

`--update` writes a timestamp to `~/.cache/agent-sandbox/update-stamp`
(or under `$XDG_CACHE_HOME`) and passes it to the build as `AGENT_CACHE_BUST`,
which rebuilds the agent layers and the entrypoint copy; the base, apt and
feature layers stay cached. Because the stamp persists and is passed on every
run, later runs keep using the refreshed layers instead of matching the stale
ones.

## Security model

### Rootless Docker (recommended)

In rootless mode, the Docker daemon runs without root privileges. Container UID 0 maps to your host user, making privilege escalation impossible at the kernel level.

The sandbox uses `network_mode: host` for convenience since the container's network namespace is already user-namespaced.

Security properties:
- Container root = your host user (no privilege escalation possible)
- Claude Code runs as non-root `dev` user inside the container
- Only your project directory is exposed, not your home directory or system files
- No git/SSH credentials are passed to the container
- ACLs ensure both the host user and container user can read/write project files

### Rootful Docker

In rootful mode, the Docker daemon runs as root. The sandbox applies additional hardening:

- **No host networking** (default): Uses Docker's default bridge network instead of `network_mode: host`. The container can reach the internet (for Anthropic API calls) but cannot bind host ports or access host-only localhost services. Only the profile's `ports` are published. Use `--host-network` or `network = "host"` to opt in to host networking when needed.
- **`no-new-privileges`**: Prevents any process in the container from gaining additional privileges via setuid/setgid binaries.
- **Minimal capabilities**: Drops all Linux capabilities except the minimum needed for container startup (`CHOWN`, `SETUID`, `SETGID`, `FOWNER`, `DAC_OVERRIDE`). These are used only by the entrypoint script; Claude Code runs as non-root and has no capabilities.

Security properties:
- Claude Code runs as non-root `dev` user inside the container
- Only your project directory is exposed, not your home directory or system files
- No git/SSH credentials are passed to the container
- No host network access
- No capability escalation possible

## What's inside the container

Every image:

- Ubuntu 24.04 (`base = "ubuntu"`) or CUDA 13.0 + cuDNN on Ubuntu 24.04 (`base = "cuda"`)
- Node.js 22
- git, git-lfs, curl, wget, build-essential, cmake, audio tools for voice mode
- The profile's agents: Claude Code (native binary with voice support),
  opencode (native binary), Codex (`@openai/codex`)
- The profile's features (see [Features](#features))

A bare `python3` interpreter is always present as a dependency of the
NodeSource `nodejs` package; the `python` feature adds pip, venv and uv.

## File structure

```
├── agent-sandbox               # Wrapper: flags, credential staging, Docker mode detection
├── sandbox-config.py           # Profiles: config dialog, selection, Dockerfile/Compose generation
├── prepare-auth.py             # Host-side login staging and API-key filtering
├── entrypoint.sh               # Permission setup, auth forwarding, ALSA→PulseAudio routing, CUDA MPS
├── base/                       # Install steps every image runs (apt, Node.js)
├── agents/                     # One install script per agent
├── features/                   # Built-in features (install.sh + feature.toml)
├── docker-compose.yml          # Shared configuration (mounts, env, agent volumes)
├── docker-compose.rootless.yml # Rootless override: host networking
├── docker-compose.rootful.yml  # Rootful override: security hardening
├── tests/test_sandbox.py       # Credential, profile and launch regression checks
└── README.md
```

## Customization

### Adding tools to an image

Add a feature (see [Features](#features)) and enable it in a profile, for
example `~/.config/agent-sandbox/features/torch/install.sh`:

```bash
#!/bin/bash
set -euo pipefail
pip install torch torchvision --break-system-packages
```

with `requires = ["python"]` in its `feature.toml`.

### Disabling voice mode

Remove the PulseAudio-related volumes and environment variables from `docker-compose.yml`, and the `sox`/`alsa`/`pulseaudio` packages from `base/apt.sh`.

## Development checks

```bash
bash -n agent-sandbox entrypoint.sh base/*.sh agents/*.sh features/*/install.sh
python3 -B -m unittest discover -s tests -v
```

Tests use synthetic credentials and mocked container commands. Compose rendering
is also checked when Docker Compose is installed; no Docker daemon or API calls
are needed.
