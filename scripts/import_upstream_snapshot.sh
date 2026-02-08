#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/import_upstream_snapshot.sh [repo_url] [commit_sha]
# Defaults target the upstream requested in task comments.

REPO_URL="${1:-https://github.com/Akhileshal96/Trident-Trade-Bot-TL}"
COMMIT_SHA="${2:-0e749c7364649ece65c46f8b8f5b023f040bf82a}"
TARGET_DIR="$(pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "Cloning $REPO_URL into temp dir..."
git clone --no-checkout "$REPO_URL" "$TMP_DIR/repo"

cd "$TMP_DIR/repo"
git checkout "$COMMIT_SHA"

# Copy snapshot content into target repo root, preserving target .git directory.
# shellcheck disable=SC2035
rsync -a --delete \
  --exclude ".git" \
  "$TMP_DIR/repo/" "$TARGET_DIR/"

echo "Imported snapshot $COMMIT_SHA from $REPO_URL into $TARGET_DIR"
