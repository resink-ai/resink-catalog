#!/usr/bin/env sh
# Build the web UI into src/resink_catalog/static (served by `resink-catalog serve`, shipped in the wheel).
# Needs Node 20+; the first run installs web/node_modules from the npm registry.
set -eu
cd "$(dirname "$0")/../web"
npm ci
npm test
npm run build
