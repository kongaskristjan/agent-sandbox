#!/bin/bash
# Packages every image needs; the features' apt packages come as arguments.
set -euo pipefail
apt-get update
apt-get install -y \
    git git-lfs curl wget sudo gosu acl \
    build-essential cmake \
    sox libsox-fmt-all alsa-utils pulseaudio-utils libasound2-plugins \
    "$@"
rm -rf /var/lib/apt/lists/*
