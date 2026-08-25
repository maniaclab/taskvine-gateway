#!/bin/sh
set -e
eval "$(pixi shell-hook --manifest-path /app/pixi.toml --shell bash)"
exec vine_worker "$@"
