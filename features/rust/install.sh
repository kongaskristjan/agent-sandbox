#!/bin/bash
# Installed into dev's home rather than /usr/local so the sandbox user owns it
# and can `rustup update` / `cargo install` at runtime.
set -euo pipefail
curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --profile minimal \
        --default-toolchain stable --component rustfmt --component clippy
chown -R dev:dev "$RUSTUP_HOME" "$CARGO_HOME"
