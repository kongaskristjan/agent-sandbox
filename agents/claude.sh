#!/bin/bash
# Native binary (has built-in audio for voice mode).
set -euo pipefail
curl -fsSL https://claude.ai/install.sh | bash
cp /root/.local/share/claude/versions/* /usr/local/bin/claude
