#!/usr/bin/env python3
"""Profiles: config file, interactive dialog, and per-profile image generation."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys

ROOT = Path(__file__).resolve().parent
SERVICE = "agent-dev"
BASES = {
    "ubuntu": ("ubuntu:24.04", "Ubuntu 24.04 (CPU)"),
    "cuda": ("nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04", "CUDA 13 + cuDNN (NVIDIA GPU)"),
}
AGENTS = {
    "claude": ("Claude Code", ["claude", "--dangerously-skip-permissions"]),
    "opencode": ("opencode", ["opencode", "--auto"]),
    "codex": ("Codex", ["codex", "--dangerously-bypass-approvals-and-sandbox",
                        "-c", 'cli_auth_credentials_store="file"',
                        "-c", 'projects."/workspace/project".trust_level="trusted"']),
}
AGENTS_VIEW = ["claude", "agents", "--dangerously-skip-permissions"]
NETWORKS = ("bridge", "host")
PROFILE_KEYS = ("base", "features", "agents", "default_agent", "network", "ports")
PROJECT_KEYS = ("profile", "ports", "network")
DEFAULT_FEATURES = ("python", "playwright")
# Declared in docker-compose.yml; features can't reuse these names.
BASE_VOLUMES = ("claude-config", "codex-config", "opencode-config", "opencode-data")
PROJECT_FILE = ".agent-sandbox.toml"

NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
VOLUME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")
ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
APT_RE = re.compile(r"[a-z0-9][a-z0-9+.:=~-]*\Z")
PORT_RE = re.compile(r"(?:\d{1,3}(?:\.\d{1,3}){3}:)?\d{1,5}(?:-\d{1,5})?(?::\d{1,5}(?:-\d{1,5})?)?(?:/(?:tcp|udp))?\Z")


class ConfigError(Exception):
    pass


class Abort(Exception):
    pass


def xdg(variable, fallback):
    return Path(os.environ.get(variable) or Path.home() / fallback) / "agent-sandbox"


def config_dir():
    return xdg("XDG_CONFIG_HOME", ".config")


def config_path():
    return config_dir() / "config.toml"


def profiles_dir():
    return xdg("XDG_CACHE_HOME", ".cache") / "profiles"


def tilde(path):
    home = str(Path.home())
    return "~" + str(path)[len(home):] if str(path).startswith(home + os.sep) else str(path)


def load_toml(path):
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            raise ConfigError("Reading TOML requires tomli on Python older than 3.11. "
                              "Install it with: python3 -m pip install tomli") from None
    try:
        with open(path, "rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError:
        return None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    raise TypeError(value)


def dump_config(config):
    lines = []
    if config.get("default"):
        lines.append(f"default = {toml_value(config['default'])}")
    for name, profile in config["profiles"].items():
        lines += ["", f"[profiles.{name}]"]
        lines += [f"{key} = {toml_value(profile[key])}" for key in PROFILE_KEYS if key in profile]
    return "\n".join(lines).lstrip("\n") + "\n"


def save_config(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump_config(config))
    os.replace(tmp, path)


def check_type(where, key, value, kind):
    if kind is list and isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    if kind is dict and isinstance(value, dict) and all(isinstance(v, str) for v in value.values()):
        return value
    if kind is str and isinstance(value, str):
        return value
    expected = {list: "a list of strings", dict: "a table of strings", str: "a string"}[kind]
    raise ConfigError(f"{where}: '{key}' must be {expected}")


def check_ports(where, ports):
    if not isinstance(ports, list):
        raise ConfigError(f"{where}: 'ports' must be a list")
    for port in ports:
        ok = (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536) or (
            isinstance(port, str) and PORT_RE.match(port))
        if not ok:
            raise ConfigError(f"{where}: invalid port {port!r} (use 3000 or \"8080:3000\")")
    return ports


def publish_spec(port):
    return f"{port}:{port}" if isinstance(port, int) or ":" not in port.split("/")[0] else port


def check_unknown(where, data, allowed):
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(allowed)})")


def check_profile(name, raw):
    where = f"{config_path()} [profiles.{name}]"
    if not NAME_RE.match(name):
        raise ConfigError(f"{where}: profile names use lowercase letters, digits and '-'")
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a table")
    check_unknown(where, raw, PROFILE_KEYS)
    profile = {
        "base": check_type(where, "base", raw.get("base", "ubuntu"), str),
        "features": check_type(where, "features", raw.get("features", []), list),
        "agents": check_type(where, "agents", raw.get("agents", list(AGENTS)), list),
        "network": check_type(where, "network", raw.get("network", "bridge"), str),
        "ports": check_ports(where, raw.get("ports", [])),
    }
    if profile["base"] not in BASES:
        raise ConfigError(f"{where}: base must be one of {', '.join(BASES)}")
    if profile["network"] not in NETWORKS:
        raise ConfigError(f"{where}: network must be one of {', '.join(NETWORKS)}")
    unknown = [a for a in profile["agents"] if a not in AGENTS]
    if unknown or not profile["agents"]:
        raise ConfigError(f"{where}: agents must be a non-empty subset of {', '.join(AGENTS)}")
    profile["agents"] = [a for a in AGENTS if a in profile["agents"]]
    profile["default_agent"] = check_type(where, "default_agent",
                                          raw.get("default_agent", profile["agents"][0]), str)
    if profile["default_agent"] not in profile["agents"]:
        raise ConfigError(f"{where}: default_agent must be one of its agents ({', '.join(profile['agents'])})")
    return profile


def load_config():
    """Return the validated config, or None when no profile exists yet."""
    path = config_path()
    raw = load_toml(path)
    if raw is None:
        return None
    check_unknown(str(path), raw, ("default", "profiles"))
    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError(f"{path}: 'profiles' must be a table")
    config = {"default": raw.get("default"),
              "profiles": {name: check_profile(name, p) for name, p in profiles.items()}}
    if not config["profiles"]:
        return None
    if config["default"] not in config["profiles"]:
        if config["default"] is not None:
            raise ConfigError(f"{path}: default profile '{config['default']}' does not exist")
        config["default"] = next(iter(config["profiles"]))
    return config


def read_feature(directory):
    where = str(directory / "feature.toml")
    raw = load_toml(directory / "feature.toml") or {}
    check_unknown(where, raw, ("description", "requires", "apt", "env", "volumes", "compose"))
    feature = {
        "name": directory.name,
        "dir": directory,
        "description": check_type(where, "description", raw.get("description", ""), str),
        "requires": check_type(where, "requires", raw.get("requires", []), list),
        "apt": check_type(where, "apt", raw.get("apt", []), list),
        "env": check_type(where, "env", raw.get("env", {}), dict),
        "volumes": check_type(where, "volumes", raw.get("volumes", {}), dict),
        "compose": raw.get("compose", {}),
    }
    if not isinstance(feature["compose"], dict):
        raise ConfigError(f"{where}: 'compose' must be a table")
    for package in feature["apt"]:
        if not APT_RE.match(package):
            raise ConfigError(f"{where}: invalid apt package {package!r}")
    for key, value in feature["env"].items():
        if not ENV_RE.match(key) or "\n" in value:
            raise ConfigError(f"{where}: invalid env entry {key!r}")
    for volume, target in feature["volumes"].items():
        if not VOLUME_RE.match(volume) or volume in BASE_VOLUMES or not target.startswith("/"):
            raise ConfigError(f"{where}: invalid volume {volume!r} = {target!r}")
    return feature


def load_features():
    """Built-in features, overridden by same-named ones in the user config dir."""
    features = {}
    for directory in (ROOT / "features", config_dir() / "features"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            if not NAME_RE.match(path.name):
                raise ConfigError(f"{path}: feature names use lowercase letters, digits and '-'")
            if not (path / "install.sh").is_file():
                raise ConfigError(f"{path}: a feature needs an install.sh")
            features[path.name] = read_feature(path)
    return features


def feature_order(selected, features):
    """Selected features plus their requirements, dependencies first, ties by name."""
    needed, stack = set(), list(selected)
    while stack:
        name = stack.pop()
        if name not in features:
            raise ConfigError(f"unknown feature '{name}' (available: {', '.join(sorted(features))})")
        if name not in needed:
            needed.add(name)
            stack.extend(features[name]["requires"])
    order, done = [], set()
    while len(order) < len(needed):
        ready = sorted(n for n in needed - done if set(features[n]["requires"]) <= done)
        if not ready:
            raise ConfigError(f"features have circular requirements: {', '.join(sorted(needed - done))}")
        order.append(features[ready[0]])
        done.add(ready[0])
    return order


def env_instruction(env):
    pairs = [f'{key}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
             for key, value in env.items()]
    return "ENV " + " \\\n    ".join(pairs)


def render_dockerfile(profile, ordered):
    apt = sorted({package for feature in ordered for package in feature["apt"]})
    lines = [
        "# Generated by agent-sandbox; edit the profile or its features instead.",
        f"FROM {BASES[profile['base']][0]}",
        "",
        "# Each script is copied right before its RUN, so editing one only",
        "# rebuilds from that layer on.",
        "ARG SCRIPTS=/usr/local/lib/agent-sandbox/build",
        "ENV NO_UPDATE_NOTIFIER=1",
        "",
        "COPY base/apt.sh $SCRIPTS/base/",
        " ".join(["RUN bash $SCRIPTS/base/apt.sh", *apt]),
        "",
        "RUN useradd -m -s /bin/bash -G root,audio dev",
        "",
        "COPY base/node.sh $SCRIPTS/base/",
        "RUN bash $SCRIPTS/base/node.sh",
    ]
    for feature in ordered:
        name = feature["name"]
        lines += ["", f"# Feature: {name}"]
        if feature["env"]:
            lines.append(env_instruction(feature["env"]))
        lines += [f"COPY features/{name}/ $SCRIPTS/features/{name}/",
                  f"RUN bash $SCRIPTS/features/{name}/install.sh"]
    lines += [
        "",
        "# Agent layers go last: `agent-sandbox --update` changes AGENT_CACHE_BUST,",
        "# which re-runs every RUN from here on while the layers above stay cached.",
        "ARG AGENT_CACHE_BUST=0",
    ]
    for agent in profile["agents"]:
        lines += [f"COPY agents/{agent}.sh $SCRIPTS/agents/", f"RUN bash $SCRIPTS/agents/{agent}.sh"]
    lines += [
        "",
        "COPY prepare-auth.py /usr/local/lib/agent-sandbox/prepare-auth.py",
        "COPY entrypoint.sh /entrypoint.sh",
        "RUN chmod +x /entrypoint.sh",
        "",
        "WORKDIR /workspace",
        'ENTRYPOINT ["/entrypoint.sh"]',
        f"CMD {json.dumps(AGENTS[profile['default_agent']][1])}",
    ]
    return "\n".join(lines) + "\n"


def merge(target, extra, where):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value, where)
        elif isinstance(value, list) and isinstance(target.get(key), list):
            target[key] = target[key] + value
        elif key in target and target[key] != value:
            raise ConfigError(f"{where}: compose key '{key}' conflicts with another setting")
        else:
            target[key] = value


def render_compose(name, profile, ordered, context):
    service = {
        "build": {"context": str(context), "dockerfile": "Dockerfile"},
        "image": f"agent-sandbox-{name}",
    }
    if profile["base"] == "cuda":
        service["runtime"] = "nvidia"
        service["environment"] = ["NVIDIA_VISIBLE_DEVICES=all",
                                  "NVIDIA_DRIVER_CAPABILITIES=compute,utility"]
    volumes = {}
    for feature in ordered:
        for volume, target in feature["volumes"].items():
            if volumes.get(volume, target) != target:
                raise ConfigError(f"volume '{volume}' is mounted at two different paths")
            volumes[volume] = target
    if volumes:
        service["volumes"] = [f"{volume}:{target}" for volume, target in volumes.items()]
    for feature in ordered:
        merge(service, feature["compose"], feature["dir"] / "feature.toml")
    document = {"services": {SERVICE: service}}
    if volumes:
        # Plain declarations keep the existing agent-sandbox_<name> volumes' config hash.
        document["volumes"] = {volume: {} for volume in volumes}
    return document


def yaml_text(document):
    # JSON is valid YAML, and json.dumps handles all the quoting.
    return "# Generated by agent-sandbox.\n" + json.dumps(document, indent=2) + "\n"


def sync_dir(directory, files):
    """Write only changed files and drop stale ones, so concurrent runs see a stable context."""
    directory.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_bytes() != content:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(content)
            os.replace(tmp, path)
    for path in sorted(directory.rglob("*"), reverse=True):
        relative = path.relative_to(directory).as_posix()
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
        elif relative not in files:
            path.unlink()


def generate(name, profile, features):
    """Write the profile's build context and compose override; return the override path."""
    ordered = feature_order(profile["features"], features)
    directory = profiles_dir() / name
    files = {
        "Dockerfile": render_dockerfile(profile, ordered).encode(),
        "compose.yml": yaml_text(render_compose(name, profile, ordered, directory)).encode(),
        "base/apt.sh": (ROOT / "base/apt.sh").read_bytes(),
        "base/node.sh": (ROOT / "base/node.sh").read_bytes(),
        "entrypoint.sh": (ROOT / "entrypoint.sh").read_bytes(),
        "prepare-auth.py": (ROOT / "prepare-auth.py").read_bytes(),
    }
    for agent in profile["agents"]:
        files[f"agents/{agent}.sh"] = (ROOT / "agents" / f"{agent}.sh").read_bytes()
    for feature in ordered:
        for path in sorted(feature["dir"].rglob("*")):
            relative = path.relative_to(feature["dir"]).as_posix()
            if path.is_file() and relative != "feature.toml":
                files[f"features/{feature['name']}/{relative}"] = path.read_bytes()
    sync_dir(directory, files)
    return directory / "compose.yml"


def load_project(project_dir):
    path = Path(project_dir) / PROJECT_FILE
    raw = load_toml(path) or {}
    check_unknown(str(path), raw, PROJECT_KEYS)
    if "profile" in raw:
        check_type(str(path), "profile", raw["profile"], str)
    if raw.get("network", "bridge") not in NETWORKS:
        raise ConfigError(f"{path}: network must be one of {', '.join(NETWORKS)}")
    check_ports(str(path), raw.get("ports", []))
    return raw


def select_profile(config, project, flag):
    name = flag or project.get("profile") or config["default"]
    if name not in config["profiles"]:
        source = "-p" if flag else PROJECT_FILE if project.get("profile") else "default"
        raise ConfigError(f"profile '{name}' (from {source}) does not exist "
                          f"(profiles: {', '.join(config['profiles'])})")
    return name


def select_agent(name, profile, requested):
    agent = "claude" if requested == "agents" else requested or profile["default_agent"]
    if agent not in profile["agents"]:
        raise ConfigError(f"profile '{name}' does not install {agent} (it has: "
                          f"{', '.join(profile['agents'])}). Add it with 'agent-sandbox config'.")
    return agent, AGENTS_VIEW if requested == "agents" else AGENTS[agent][1]


def render_run_override(network, ports, mode):
    service = {}
    if mode == "rootless" or network == "host":
        if ports:
            print("Warning: ignoring ports under host networking; the container's ports "
                  "are already the host's.", file=sys.stderr)
        if mode == "rootful":
            service["network_mode"] = "host"
    elif ports:
        service["ports"] = [publish_spec(port) for port in ports]
    return {"services": {SERVICE: service}}


def prepare(args):
    config = load_config()
    if config is None:
        if not sys.stdin.isatty():
            raise ConfigError(f"no profiles configured in {tilde(config_path())}. "
                              "Run 'agent-sandbox config' in a terminal to create one.")
        print("No profiles yet — creating one.")
        edit_profile({"default": None, "profiles": {}}, load_features())
        config = load_config()
    project = load_project(args.project_dir)
    name = select_profile(config, project, args.profile)
    profile = config["profiles"][name]
    agent, command = select_agent(name, profile, args.agent)
    profile_compose = generate(name, profile, load_features())
    network = "host" if args.host_network else project.get("network", profile["network"])
    ports = profile["ports"] + project.get("ports", [])
    Path(args.run_compose).write_text(yaml_text(render_run_override(network, ports, args.mode)))
    status = [args.mode, f"profile {name}"]
    if profile["base"] == "cuda":
        status.append("cuda")
    if network == "host" and args.mode == "rootful":
        status.append("host networking")
    values = {"SANDBOX_AGENT": agent, "PROFILE_COMPOSE": str(profile_compose),
              "PROFILE_STATUS": ", ".join(status)}
    with open(args.env_out, "w") as stream:
        for key, value in values.items():
            stream.write(f"{key}={shlex.quote(value)}\n")
        stream.write(f"RUN_ARGS=({' '.join(map(shlex.quote, command))})\n")


# Interactive dialog -------------------------------------------------------

def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        raise Abort from None


def choose(title, options, default):
    """options: [(value, label)]; returns the chosen value."""
    values = [value for value, _ in options]
    print(title + "  " + "   ".join(f"{i}) {label}" for i, (_, label) in enumerate(options, 1)))
    while True:
        answer = ask(f"> [{values.index(default) + 1}] ")
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return values[int(answer) - 1]
        print(f"  Enter a number from 1 to {len(options)}.")


def toggle(title, items, selected):
    """items: [(name, description)]; returns the selected names in item order."""
    selected = set(selected)
    width = max(len(name) for name, _ in items)
    while True:
        print(f"{title} (toggle by number, Enter to accept):")
        for i, (name, description) in enumerate(items, 1):
            mark = "x" if name in selected else " "
            print(f"  [{mark}] {i} {name.ljust(width)}  {description}".rstrip())
        answer = ask("> ")
        if not answer:
            return [name for name, _ in items if name in selected]
        for token in answer.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(items):
                selected ^= {items[int(token) - 1][0]}
            else:
                print(f"  Ignoring '{token}': not a number from the list.")


def ask_ports(current):
    shown = " ".join(map(str, current))
    hint = f' [{shown}] ("none" to clear)' if current else ' (e.g. "3000 5173:5173", blank for none)'
    while True:
        answer = ask(f"Ports to publish{hint}: ")
        if not answer:
            return current
        if answer == "none":
            return []
        ports = [int(t) if t.isdigit() else t for t in answer.replace(",", " ").split()]
        try:
            return check_ports("ports", ports)
        except ConfigError as error:
            print(f"  {error}")


def ask_yes(prompt):
    return ask(prompt + " [y/N] ").lower() in ("y", "yes")


def edit_profile(config, features, name=None):
    new = name is None
    if new:
        default_name = "default" if "default" not in config["profiles"] else ""
        while True:
            name = ask(f"Profile name [{default_name}]: " if default_name else "Profile name: ") or default_name
            if not NAME_RE.match(name):
                print("  Use lowercase letters, digits and '-'.")
            elif name in config["profiles"]:
                print(f"  Profile '{name}' already exists.")
            else:
                break
        current = {"base": "ubuntu", "features": [f for f in DEFAULT_FEATURES if f in features],
                   "agents": list(AGENTS), "default_agent": "claude", "network": "bridge", "ports": []}
    else:
        current = config["profiles"][name]
    profile = {"base": choose("Base image:", [(k, v[1]) for k, v in BASES.items()], current["base"])}
    if features:
        items = [(n, features[n]["description"]) for n in sorted(features)]
        profile["features"] = toggle("Features", items, current["features"])
    else:
        profile["features"] = []
    while True:
        agents = toggle("Agents", [(k, v[0]) for k, v in AGENTS.items()], current["agents"])
        if agents:
            break
        print("  Select at least one agent.")
    profile["agents"] = agents
    default_agent = current["default_agent"] if current["default_agent"] in agents else agents[0]
    if len(agents) > 1:
        default_agent = choose("Default agent:", [(a, a) for a in agents], default_agent)
    profile["default_agent"] = default_agent
    profile["network"] = choose("Network:", [(n, n) for n in NETWORKS], current["network"])
    profile["ports"] = ask_ports(current["ports"])
    config["profiles"][name] = profile
    if config.get("default") not in config["profiles"] or (
            new and len(config["profiles"]) > 1 and ask_yes(f"Make '{name}' the default profile?")):
        config["default"] = name
    save_config(config)
    marker = " (default)" if config["default"] == name else ""
    print(f"Saved profile '{name}'{marker} → {tilde(config_path())}")


def describe(config):
    lines = []
    for name, profile in config["profiles"].items():
        agents = [a + (" (default)" if a == profile["default_agent"] else "") for a in profile["agents"]]
        lines += [f"{'*' if name == config['default'] else ' '} {name}",
                  f"    base: {profile['base']}   network: {profile['network']}",
                  f"    features: {', '.join(profile['features']) or '-'}",
                  f"    agents: {', '.join(agents)}",
                  f"    ports: {' '.join(map(str, profile['ports'])) or '-'}"]
    return "\n".join(lines)


def pick_profile(config, title):
    names = list(config["profiles"])
    for i, name in enumerate(names, 1):
        print(f"  {i}) {name}")
    answer = ask(f"{title} (number, blank to cancel): ")
    if answer.isdigit() and 1 <= int(answer) <= len(names):
        return names[int(answer) - 1]
    return None


def config_menu(config, features):
    if config is None:
        print("No profiles yet — creating one.")
        edit_profile({"default": None, "profiles": {}}, features)
        return
    while True:
        print(f"\nProfiles in {tilde(config_path())} (* = default):")
        print(describe(config))
        action = ask("1) Add  2) Edit  3) Delete  4) Set default  5) Quit\n> ")
        if action == "1":
            edit_profile(config, features)
        elif action == "2":
            name = pick_profile(config, "Edit which profile")
            if name:
                edit_profile(config, features, name)
        elif action == "3":
            name = pick_profile(config, "Delete which profile")
            if name and ask_yes(f"Delete profile '{name}'?"):
                del config["profiles"][name]
                if config["default"] == name:
                    config["default"] = next(iter(config["profiles"]), None)
                save_config(config)
                shutil.rmtree(profiles_dir() / name, ignore_errors=True)
                print(f"Deleted profile '{name}'.")
                if not config["profiles"]:
                    return
        elif action == "4":
            name = pick_profile(config, "Default profile")
            if name:
                config["default"] = name
                save_config(config)
        elif action in ("", "5", "q"):
            return
        else:
            print("  Enter a number from 1 to 5.")


def main():
    parser = argparse.ArgumentParser(prog="agent-sandbox")
    commands = parser.add_subparsers(dest="command", required=True)
    config_parser = commands.add_parser("config", help="add, edit or delete profiles")
    config_parser.add_argument("--list", action="store_true", help="list profiles and exit")
    prep = commands.add_parser("prepare", help="(internal) generate the profile for a run")
    prep.add_argument("--project-dir", required=True)
    prep.add_argument("--profile", default="")
    prep.add_argument("--agent", default="", choices=["", "agents", *AGENTS])
    prep.add_argument("--mode", required=True, choices=["rootful", "rootless"])
    prep.add_argument("--host-network", action="store_true")
    prep.add_argument("--run-compose", required=True)
    prep.add_argument("--env-out", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args)
        elif args.list:
            config = load_config()
            print(describe(config) if config else
                  f"No profiles in {tilde(config_path())}; run 'agent-sandbox config'.")
        else:
            config_menu(load_config(), load_features())
    except ConfigError as error:
        sys.exit(f"Error: {error}")
    except (Abort, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
