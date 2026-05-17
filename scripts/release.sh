#!/usr/bin/env bash
set -euo pipefail

version="${1:-}"
if [[ -z "$version" ]]; then
  echo "Usage: scripts/release.sh <version>" >&2
  exit 1
fi

if ! command -v git-cliff >/dev/null 2>&1; then
  echo "git-cliff is required. Install it first: https://git-cliff.org/docs/installation" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree must be clean before releasing." >&2
  exit 1
fi

tag="v$version"
printf "%s\n" "$version" > VERSION
git-cliff --tag "$tag" --output CHANGELOG.md

git add VERSION CHANGELOG.md
git commit -m "chore: bump version to $version — 升级版本到$version"
git tag "$tag"

echo "Release commit and tag created: $tag"
echo "Review, then push with:"
echo "  git push"
echo "  git push origin $tag"
