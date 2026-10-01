#!/bin/bash
# Playwright CLI + browser + OS deps, all from the playwright-core bundled in
# @playwright/cli so revisions can't drift; PLAYWRIGHT_CLI_VERSION is the pin.
# Only chrome-headless-shell: full Chrome-for-Testing SIGTRAPs under this
# sandbox's security profile. The global config's browserName "chromium" (no
# channel) makes headless launches use the shell.
# The browser is baked into /opt/playwright-seed because the named volume at
# PLAYWRIGHT_BROWSERS_PATH masks image content; entrypoint.sh syncs it in.
set -euo pipefail

npm install -g "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}"
pkg="$(npm root -g)/@playwright/cli"

apt-get update
node "$pkg/node_modules/playwright-core/cli.js" install-deps chromium
rm -rf /var/lib/apt/lists/*

PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-seed \
    playwright-cli install-browser --only-shell chromium
chmod -R a+rX /opt/playwright-seed

mkdir -p /home/dev/.playwright
echo '{"browser": {"browserName": "chromium"}}' > /home/dev/.playwright/cli.config.json
chown -R dev:dev /home/dev/.playwright
