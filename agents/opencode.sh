#!/bin/bash
# Standalone native binary, copied to /usr/local/bin so dev can run it.
set -euo pipefail
curl -fsSL https://opencode.ai/install | bash
cp /root/.opencode/bin/opencode /usr/local/bin/opencode
