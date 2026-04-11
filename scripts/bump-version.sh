#!/usr/bin/env bash
# Increment build number in version.json
set -euo pipefail
VERSION_FILE="$(dirname "$0")/../version.json"
BUILD=$(jq '.build + 1' "$VERSION_FILE")
jq ".build = $BUILD" "$VERSION_FILE" > "$VERSION_FILE.tmp" && mv "$VERSION_FILE.tmp" "$VERSION_FILE"
MAJOR=$(jq -r '.major' "$VERSION_FILE")
MINOR=$(jq -r '.minor' "$VERSION_FILE")
echo "$MAJOR.$MINOR.$BUILD"
