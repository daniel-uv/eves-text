#!/usr/bin/env bash
# Запуск редактора из исходников (без установки).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec python3 -m eves_text "$@"
