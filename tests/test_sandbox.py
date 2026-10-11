"""Regression checks using synthetic credentials; no Docker daemon or API calls."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import tomllib
except ImportError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_auth", ROOT / "prepare-auth.py")
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)
LOGIN = {
    "auth_mode": "chatgpt",
    "OPENAI_API_KEY": "stored-api-secret",
    "tokens": {
        "access_token": "oauth-access",
        "refresh_token": "oauth-refresh",
        "id_token": "oauth-id",
        "account_id": "account",
    },
    "last_refresh": "2026-09-21T00:00:00Z",
}
CONFIG = '''# Host preferences
model = "test-model"
profile = "work"
model_reasoning_effort = "high"
[profiles.work]
model = "work-model"
[mcp_servers.example]
command = "example-mcp"
[mcp_servers.example.env]
OPENAI_API_KEY = "config-api-secret"
NORMAL_SETTING = "keep-me"
[model_providers.custom]
base_url = "https://example.invalid/v1"
experimental_bearer_token = "provider-secret"
'''


class AuthTests(unittest.TestCase):
    def test_codex_oauth_survives_without_api_key(self):
        result = auth.filter_auth("codex", LOGIN)
        self.assertEqual(result["tokens"], LOGIN["tokens"])
        self.assertEqual(result["last_refresh"], LOGIN["last_refresh"])
        self.assertNotIn("OPENAI_API_KEY", result)

    def test_codex_api_only_and_invalid_data_are_not_forwarded(self):
        for data in ({"OPENAI_API_KEY": "secret"}, [], None, {"tokens": "invalid"}):
            self.assertEqual(auth.filter_auth("codex", data), {})

    def test_explicit_api_key_opt_in(self):
        self.assertEqual(auth.filter_auth("codex", LOGIN, True), LOGIN)

    def test_other_agents_exclude_keys(self):
        self.assertEqual(auth.filter_auth("claude", {
            "claudeAiOauth": {"accessToken": "oauth"}, "primaryApiKey": "secret"
        }), {"claudeAiOauth": {"accessToken": "oauth"}})
        self.assertEqual(auth.filter_auth("opencode", {
            "anthropic": {"type": "oauth", "access": "oauth"},
            "openai": {"type": "api", "key": "secret"},
        }), {"anthropic": {"type": "oauth", "access": "oauth"}})

    def test_config_filters_nested_api_keys(self):
        self.assertEqual(auth.filter_auth("config", {
            "model": "local/model",
            "provider": {"local": {"options": {"apiKey": "secret", "baseURL": "url"}}},
            "mcpServers": {"test": {"env": {"ANTHROPIC_API_KEY": "secret"}}},
        }), {
            "model": "local/model",
            "provider": {"local": {"options": {"baseURL": "url"}}},
            "mcpServers": {"test": {"env": {}}},
        })

    def test_codex_toml_filter_preserves_config_types_and_quoted_keys(self):
        contents = '''
"custom.🚀" = { enabled = true, values = [1, 0.25, "line\\nquote\\\""], api_key = "secret" }
date = 2026-09-22
time = 12:30:00
timestamp = 2026-09-22T12:30:00Z
[[servers]]
name = "one"
[[servers]]
name = "two"
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.toml"
            destination = Path(directory) / "filtered.toml"
            source.write_text(contents)
            auth.stage_codex_config(source, destination)
            expected = tomllib.loads(contents)
            del expected["custom.🚀"]["api_key"]
            self.assertEqual(tomllib.loads(destination.read_text()), expected)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_codex_config_falls_back_when_tomllib_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.toml"
            destination = Path(directory) / "filtered.toml"
            source.write_text(CONFIG)
            # Exercise the older-Python import path using the available TOML
            # parser, without requiring an extra dependency on newer Python.
            with patch.dict(sys.modules, {"tomllib": None, "tomli": tomllib}):
                auth.stage_codex_config(source, destination)
            config = tomllib.loads(destination.read_text())
            self.assertEqual(config["profile"], "work")
            self.assertNotIn("secret", destination.read_text())

    def test_codex_config_explains_missing_toml_dependency(self):
        with patch.dict(sys.modules, {"tomllib": None, "tomli": None}):
            with self.assertRaisesRegex(SystemExit, "python3 -m pip install tomli"):
                auth.stage_codex_config(Path("unused"), Path("unused"))


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.host = self.base / "host with spaces"
        self.host.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.host),
            "CODEX_HOME": str(self.host / "custom codex"),
            "XDG_CACHE_HOME": str(self.base / "cache"),
            "TMPDIR": str(self.base),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "AGENT_SANDBOX_MODE": "rootful",
            "ANTHROPIC_API_KEY": "environment-anthropic-secret",
            "OPENAI_API_KEY": "environment-openai-secret",
            "CAPTURE": str(self.base / "capture.json"),
        }
        self.write(self.host / "custom codex/auth.json", LOGIN)
        self.setup = self.host / "custom codex"
        (self.setup / "config.toml").write_text(CONFIG)
        (self.setup / "work.config.toml").write_text('model = "profile-model"\n')
        (self.setup / "AGENTS.md").write_text("Personal instructions\n")
        (self.setup / "rules").mkdir()
        (self.setup / "rules/default.rules").write_text('prefix_rule(pattern=["git"], decision="allow")\n')
        (self.setup / "agents").mkdir()
        (self.setup / "agents/reviewer.toml").write_text(CONFIG)
        (self.setup / "skills").mkdir()
        shared = self.host / ".agents/skills/example"
        shared.mkdir(parents=True)
        (shared / "SKILL.md").write_text("Shared skill\n")
        (shared / "helper.sh").write_text("#!/bin/sh\nexit 0\n")
        (shared / "helper.sh").chmod(0o755)
        (self.setup / "skills/linked").symlink_to(shared, target_is_directory=True)
        (self.setup / "history.jsonl").write_text("private host history\n")
        (self.setup / "sessions").mkdir()
        (self.setup / "sessions/session.jsonl").write_text("private session\n")
        self.write(self.host / ".claude/.credentials.json", {
            "claudeAiOauth": {"accessToken": "claude-oauth"}, "primaryApiKey": "secret"
        })
        self.write(self.host / ".claude.json", {"primaryApiKey": "secret"})
        self.write(self.host / ".local/share/opencode/auth.json", {
            "openai": {"type": "api", "key": "secret"},
            "anthropic": {"type": "oauth", "access": "opencode-oauth"},
        })
        self.write(self.host / ".config/opencode/opencode.json", {
            "provider": {"openai": {"options": {"apiKey": "secret"}}}
        })
        self.executable("docker", '''#!/usr/bin/env python3
import json, os, pathlib, sys
if "build" in sys.argv:
    pathlib.Path(os.environ["CAPTURE"]).with_suffix(".build.json").write_text(json.dumps(sys.argv[1:]))
if "run" in sys.argv:
    names = ["CLAUDE_CREDENTIALS_FILE", "CLAUDE_CONFIG_FILE", "OPENCODE_AUTH_FILE",
             "OPENCODE_CONFIG_FILE", "CODEX_AUTH_FILE"]
    paths = {key: os.environ[key] for key in names}
    data = {"args": sys.argv[1:], "paths": paths,
            "files": {key: pathlib.Path(path).read_text() for key, path in paths.items()},
            "modes": {key: pathlib.Path(path).stat().st_mode & 0o777 for key, path in paths.items()},
            "env": dict(os.environ)}
    setup = pathlib.Path(os.environ["CODEX_SETUP_DIR"])
    data["setup"] = {str(path.relative_to(setup)): path.read_text()
                     for path in setup.rglob("*") if path.is_file()}
    data["setup_links"] = [str(path) for path in setup.rglob("*") if path.is_symlink()]
    pathlib.Path(os.environ["CAPTURE"]).write_text(json.dumps(data))
''')

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def executable(self, name, contents):
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def launch(self, *args, success=True):
        result = subprocess.run([str(ROOT / "agent-sandbox"), *args, str(self.host)],
                                env=self.env, capture_output=True, text=True)
        if not success:
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(Path(self.env["CAPTURE"]).exists())
            return result
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(Path(self.env["CAPTURE"]).read_text())

    def test_codex_launch_private_staging_and_cleanup(self):
        data = self.launch("--codex")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", data["args"])
        self.assertEqual(data["env"]["SANDBOX_AGENT"], "codex")
        self.assertEqual(data["env"]["SANDBOX_ANTHROPIC_API_KEY"], "")
        self.assertEqual(data["env"]["SANDBOX_OPENAI_API_KEY"], "")
        self.assertEqual(json.loads(data["files"]["CODEX_AUTH_FILE"])["tokens"], LOGIN["tokens"])
        self.assertNotIn("stored-api-secret", str(data["files"]))
        self.assertEqual(data["files"]["CLAUDE_CREDENTIALS_FILE"], "")
        self.assertEqual(data["files"]["OPENCODE_AUTH_FILE"], "")
        self.assertTrue(all(mode == 0o600 for mode in data["modes"].values()))
        self.assertTrue(all(not Path(path).exists() for path in data["paths"].values()))
        self.assertEqual(json.loads((self.host / "custom codex/auth.json").read_text()), LOGIN)

    def test_default_codex_home(self):
        del self.env["CODEX_HOME"]
        self.write(self.host / ".codex/auth.json", LOGIN)
        (self.host / ".codex/config.toml").write_text('model = "default-home-model"\n')
        data = self.launch("--codex")
        self.assertIn("oauth-access", data["files"]["CODEX_AUTH_FILE"])
        self.assertIn("default-home-model", data["setup"]["codex/config.toml"])

    def test_codex_setup_is_copied_and_filtered(self):
        data = self.launch("--codex")
        setup = data["setup"]
        config = tomllib.loads(setup["codex/config.toml"])
        self.assertEqual(config["profiles"]["work"]["model"], "work-model")
        self.assertEqual(config["mcp_servers"]["example"]["env"], {"NORMAL_SETTING": "keep-me"})
        self.assertEqual(config["model_providers"]["custom"], {"base_url": "https://example.invalid/v1"})
        self.assertIn("profile-model", setup["codex/work.config.toml"])
        self.assertEqual(setup["codex/AGENTS.md"], "Personal instructions\n")
        self.assertIn("prefix_rule", setup["codex/rules/default.rules"])
        self.assertEqual(setup["codex/skills/linked/SKILL.md"], "Shared skill\n")
        self.assertEqual(setup["agents/skills/example/SKILL.md"], "Shared skill\n")
        self.assertFalse(data["setup_links"])
        self.assertNotIn("secret", str(setup))
        self.assertNotIn("private", str(setup))
        self.assertNotIn("codex/auth.json", setup)
        self.assertFalse(Path(data["env"]["CODEX_SETUP_DIR"]).exists())
        self.assertEqual((self.setup / "config.toml").read_text(), CONFIG)

    def test_missing_codex_setup_leaves_host_untouched(self):
        missing = self.host / "missing codex"
        self.env["CODEX_HOME"] = str(missing)
        data = self.launch("--codex")
        self.assertFalse(missing.exists())
        self.assertFalse(any(path.startswith("codex/") for path in data["setup"]))

    def test_malformed_codex_config_fails_before_docker(self):
        (self.setup / "config.toml").write_text("[broken")
        self.launch("--codex", success=False)
        self.assertFalse(list(self.base.glob("agent-sandbox-auth.*")))

    def test_api_keys_require_opt_in(self):
        data = self.launch("--codex", "--api-keys")
        self.assertEqual(json.loads(data["files"]["CODEX_AUTH_FILE"]), LOGIN)
        self.assertEqual(data["env"]["SANDBOX_OPENAI_API_KEY"], "environment-openai-secret")
        self.assertEqual(data["setup"]["codex/config.toml"], CONFIG)

    def test_missing_malformed_and_api_only_codex_login(self):
        path = self.host / "custom codex/auth.json"
        for contents in (None, "{bad json", '{"OPENAI_API_KEY": "secret"}'):
            with self.subTest(contents=contents):
                if contents is None:
                    path.unlink()
                else:
                    path.write_text(contents)
                self.assertEqual(self.launch("--codex")["files"]["CODEX_AUTH_FILE"], "")

    def test_conflicting_options(self):
        for args in (("--codex", "--opencode"), ("--codex", "--agents"),
                     ("--codex", "--port", "8080"), ("--codex", "--gpu", "--rust")):
            with self.subTest(args=args):
                self.launch(*args, success=False)

    def test_claude_and_opencode_defaults(self):
        for args, agent in (((), "claude"), (("--agents",), "claude"), (("--opencode",), "opencode")):
            with self.subTest(agent=agent, args=args):
                data = self.launch(*args)
                self.assertEqual(data["env"]["SANDBOX_AGENT"], agent)
                self.assertNotIn("secret", str(data["files"]))
                self.assertEqual(data["files"]["CODEX_AUTH_FILE"], "")
                self.assertEqual(data["setup"], {})

    def test_legacy_claude_key_is_opt_in(self):
        (self.host / ".claude/.credentials.json").unlink()
        self.write(self.host / ".claude/config.json", {"primaryApiKey": "legacy-secret"})
        self.env["ANTHROPIC_API_KEY"] = ""
        self.launch(success=False)
        data = self.launch("--api-keys")
        self.assertEqual(data["env"]["SANDBOX_ANTHROPIC_API_KEY"], "legacy-secret")

    def test_update_stamp_persists_as_agent_cache_bust(self):
        build = Path(self.env["CAPTURE"]).with_suffix(".build.json")
        self.launch()
        self.assertIn("AGENT_CACHE_BUST=0", json.loads(build.read_text()))
        self.launch("--update")
        stamp = (self.base / "cache/agent-sandbox/update-stamp").read_text().strip()
        self.launch()
        self.assertIn(f"AGENT_CACHE_BUST={stamp}", json.loads(build.read_text()))
        for dockerfile in ("Dockerfile", "Dockerfile.cuda", "Dockerfile.rust"):
            self.assertEqual((ROOT / dockerfile).read_text().count("ARG AGENT_CACHE_BUST=0"), 1)

    def test_macos_claude_keychain_login(self):
        self.executable("uname", "#!/bin/bash\necho Darwin\n")
        self.executable("security", '''#!/bin/bash
echo '{"claudeAiOauth":{"accessToken":"keychain-oauth"},"primaryApiKey":"secret"}'
''')
        data = self.launch()
        self.assertEqual(json.loads(data["files"]["CLAUDE_CREDENTIALS_FILE"]), {
            "claudeAiOauth": {"accessToken": "keychain-oauth"}
        })
        self.assertFalse(list(self.base.glob("agent-sandbox-auth.*")))

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is not installed")
    def test_rendered_compose_all_images_and_modes(self):
        docker = shutil.which("docker")
        # The fake HOME hides the compose CLI plugin; keep the real docker config.
        docker_config = os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker"))
        for mode in ("rootful", "rootless"):
            self.env["AGENT_SANDBOX_MODE"] = mode
            for image in ((), ("--gpu",), ("--rust",)):
                with self.subTest(mode=mode, image=image):
                    data = self.launch("--codex", *image)
                    args = data["args"][:data["args"].index("run")]
                    result = subprocess.run([docker, *args, "config", "--format", "json"],
                                            env={**data["env"], "DOCKER_CONFIG": docker_config},
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    rendered = json.loads(result.stdout)
                    self.assertEqual(rendered["name"], "agent-sandbox")
                    service = rendered["services"]["agent-dev"]
                    self.assertEqual(service["environment"]["OPENAI_API_KEY"], "")
                    self.assertEqual(service["environment"]["ANTHROPIC_API_KEY"], "")
                    self.assertEqual(service["environment"]["CODEX_HOME"], "/home/dev/.codex")
                    mounts = {m["target"]: m for m in service["volumes"]}
                    self.assertTrue(mounts["/tmp/host-codex-auth.json"]["read_only"])
                    self.assertTrue(mounts["/tmp/host-codex-setup"]["read_only"])
                    self.assertEqual(mounts["/tmp/host-codex-setup"]["source"], data["env"]["CODEX_SETUP_DIR"])
                    self.assertEqual(mounts["/home/dev/.codex"]["type"], "volume")

    def run_entrypoint(self, host_login, persisted=None, api_keys="false", setup=False, config=None,
                       agent="codex", opencode_config=None):
        sandbox = self.base / "container"
        sandbox.mkdir(exist_ok=True)
        for directory in ("home/dev/.codex", "home/dev/.config/opencode", "run/user", "tmp", "etc"):
            (sandbox / directory).mkdir(parents=True, exist_ok=True)
        if host_login is not None:
            self.write(sandbox / "tmp/host-codex-auth.json", host_login)
        if persisted is not None:
            self.write(sandbox / "home/dev/.codex/auth.json", persisted)
        (sandbox / "home/dev/.codex/local.config.toml").write_text(CONFIG)
        (sandbox / "home/dev/.codex/history.jsonl").write_text("sandbox history\n")
        if config is not None:
            (sandbox / "home/dev/.codex/config.toml").write_text(config)
        if opencode_config is not None:
            (sandbox / "home/dev/.config/opencode/opencode.json").write_text(opencode_config)
        if setup:
            destination = sandbox / "tmp/host-codex-setup"
            auth.stage_codex_setup(self.setup, destination / "codex", api_keys == "true")
            shutil.copytree(self.host / ".agents", destination / "agents")
        for command in ("chown", "setfacl"):
            self.executable(command, "#!/bin/bash\nexit 0\n")
        self.executable("gosu", '#!/bin/bash\nprintf "%s\\n" "$*" >> "$GOSU_LOG"\n')
        script = (ROOT / "entrypoint.sh").read_text()
        for prefix in ("/home/dev", "/opt/", "/workspace/", "/tmp/host-", "/run/user", "/etc/asound.conf"):
            script = script.replace(prefix, str(sandbox) + prefix)
        script = script.replace("/usr/local/lib/agent-sandbox/prepare-auth.py", str(ROOT / "prepare-auth.py"))
        result = subprocess.run(["bash", "-c", script, "entrypoint", "codex", "--dangerously-bypass-approvals-and-sandbox"],
                                env={**self.env, "SANDBOX_AGENT": agent, "SANDBOX_API_KEYS": api_keys,
                                     "AGENT_SANDBOX_NO_MPS": "1",
                                     "GOSU_LOG": str(self.base / "gosu.log")},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return sandbox / "home/dev/.codex/auth.json"

    def test_entrypoint_copies_codex_login_and_setup(self):
        path = self.run_entrypoint(auth.filter_auth("codex", LOGIN), setup=True)
        self.assertEqual(json.loads(path.read_text())["tokens"], LOGIN["tokens"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        calls = (self.base / "gosu.log").read_text()
        self.assertNotIn("mcp", calls)
        self.assertIn("dev codex --dangerously-bypass-approvals-and-sandbox", calls)
        self.assertEqual(tomllib.loads((path.parent / "config.toml").read_text())["profile"], "work")
        self.assertEqual((path.parent / "AGENTS.md").read_text(), "Personal instructions\n")
        self.assertTrue(os.access(path.parent / "skills/linked/helper.sh", os.X_OK))
        self.assertEqual((path.parent.parent / ".agents/skills/example/SKILL.md").read_text(), "Shared skill\n")
        self.assertEqual((path.parent / "history.jsonl").read_text(), "sandbox history\n")

    def test_entrypoint_removes_stale_codex_playwright_mcp(self):
        self.run_entrypoint(None, config='[mcp_servers.playwright]\ncommand = "playwright-mcp"\n')
        self.assertIn("codex mcp remove playwright", (self.base / "gosu.log").read_text())

    def test_entrypoint_keeps_other_codex_playwright_mcp(self):
        self.run_entrypoint(None, config='[mcp_servers.playwright]\ncommand = "npx"\n')
        self.assertNotIn("mcp remove", (self.base / "gosu.log").read_text())

    def test_entrypoint_removes_stale_opencode_playwright_mcp(self):
        # Shaped like `opencode mcp add` output, trailing comma included.
        self.run_entrypoint(None, agent="opencode", opencode_config="""{
  "model": "keep-me",
  "mcp": {
    "playwright": {"type": "local", "command": ["playwright-mcp", "--headless"]},
    "other": {"type": "local", "command": ["other-mcp"]}
  },
}""")
        config = json.loads((self.base / "container/home/dev/.config/opencode/opencode.json").read_text())
        self.assertEqual(config["model"], "keep-me")
        self.assertEqual(list(config["mcp"]), ["other"])

    def test_entrypoint_keeps_sandbox_login_without_host_login(self):
        path = self.run_entrypoint(None, persisted=LOGIN)
        self.assertEqual(json.loads(path.read_text())["tokens"], LOGIN["tokens"])
        self.assertNotIn("OPENAI_API_KEY", json.loads(path.read_text()))
        config = (path.parent / "local.config.toml").read_text()
        self.assertEqual(tomllib.loads(config)["profile"], "work")
        self.assertNotIn("secret", config)

    def test_entrypoint_removes_old_api_only_auth_without_opt_in(self):
        path = self.run_entrypoint(None, persisted={"OPENAI_API_KEY": "secret"})
        self.assertFalse(path.exists())

    def test_entrypoint_api_key_login_requires_opt_in(self):
        self.run_entrypoint(LOGIN, api_keys="true")
        calls = (self.base / "gosu.log").read_text()
        self.assertIn('codex -c cli_auth_credentials_store="file" login --with-api-key', calls)
        self.assertNotIn("environment-openai-secret", calls)


if __name__ == "__main__":
    unittest.main()
