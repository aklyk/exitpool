#!/usr/bin/env bash
set -euo pipefail
umask 077
mkdir -p /run/happ
exec dbus-run-session -- python3 /app/manager.py "$@"
