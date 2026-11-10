#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.9+ est requis : https://www.python.org/downloads/'
  exit 1
fi
exec python3 radar.py --open "$@"
