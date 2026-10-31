#!/usr/bin/env bash
# Pure `proj init` demo: scaffold a fresh project in the .testproj sandbox and
# LEAVE IT for you to browse (note: `pixi run e2e` rebuilds .testproj from
# scratch, wiping the demo).
# Usage: bash scripts/init-demo.sh   (always uses the .testproj sandbox)
set -u
cd "$(dirname "$0")/.."

DIR=.testproj
if [ -f "$DIR/.proj.sqlite" ]; then
  echo "$DIR already has a project — rebuilding (any edits in it are lost)" >&2
fi
rm -rf "$DIR"
mkdir -p "$DIR"
git -C "$DIR" init -q
if [ -x .pixi/envs/dev/bin/proj ]; then PROJ="$(pwd)/.pixi/envs/dev/bin/proj"; else PROJ=proj; fi

( cd "$DIR" && $PROJ init )
echo
echo "Demo left in $DIR (gitignored; 'pixi run e2e' will wipe it)."
