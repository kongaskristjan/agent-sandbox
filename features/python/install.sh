#!/bin/bash
set -euo pipefail
curl -LsSf https://astral.sh/uv/install.sh | sh
cp /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/
