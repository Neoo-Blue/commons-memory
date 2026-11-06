#!/bin/bash
# Publish a Commons release without GitHub Actions: build the wheel and sdist from the tag on the homelab CI box
# (ssh alias `ci`, LXC 123; any box with uv works: CI_HOST=...), then create the GitHub release with that version's
# CHANGELOG section and attach both files. The one-line installers and `commons join` download the latest release.
# Usage: scripts/release-local.sh v0.37.1 [--not-latest]      (run from the repo, after pushing the tag)
set -euo pipefail
TAG="${1:?usage: scripts/release-local.sh vX.Y.Z [--not-latest]}"
LATEST="--latest"
[ "${2:-}" = "--not-latest" ] && LATEST="--latest=false"
HOST="${CI_HOST:-ci}"
VER="${TAG#v}"
cd "$(git rev-parse --show-toplevel)"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || { echo "no tag $TAG here: tag and push it first" >&2; exit 1; }
[ "$(git show "$TAG:pyproject.toml" | sed -n 's/^version = "\(.*\)"/\1/p')" = "$VER" ] || { echo "$TAG's pyproject.toml is not $VER" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
REMOTE="/tmp/commons-release-$VER"
ssh -o BatchMode=yes "$HOST" "rm -rf $REMOTE && mkdir -p $REMOTE"
git archive "$TAG" | ssh -o BatchMode=yes "$HOST" "tar -x -C $REMOTE"
ssh -o BatchMode=yes "$HOST" "cd $REMOTE && uv build --quiet && ls dist"
scp -q "$HOST:$REMOTE/dist/*" "$WORK/"
ssh -o BatchMode=yes "$HOST" "rm -rf $REMOTE"
ls "$WORK"/commons_memory-"$VER"-py3-none-any.whl "$WORK"/commons_memory-"$VER".tar.gz >/dev/null
# the release notes are this version's CHANGELOG section, title included
awk -v t="## $TAG:" 'index($0, t) == 1 {on=1; print; next} /^## v/ && on {exit} on' <(git show "$TAG:CHANGELOG.md") > "$WORK/notes.md"
TITLE="$(head -1 "$WORK/notes.md" | sed 's/^## //')"
[ -n "$TITLE" ] || { echo "no CHANGELOG section for $TAG" >&2; exit 1; }
tail -n +2 "$WORK/notes.md" > "$WORK/body.md"
if gh release view "$TAG" >/dev/null 2>&1; then
  gh release upload "$TAG" "$WORK"/*.whl "$WORK"/*.tar.gz --clobber
else
  gh release create "$TAG" "$WORK"/*.whl "$WORK"/*.tar.gz --title "$TITLE" --notes-file "$WORK/body.md" --verify-tag $LATEST
fi
echo "released $TAG: $(gh release view "$TAG" --json url --jq .url)"
