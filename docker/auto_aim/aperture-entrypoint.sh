#!/usr/bin/env bash
set -e
source /usr/local/lib/aperture/environment.sh
if [ "$#" -eq 0 ]; then set -- bash; fi
case "$1" in
    simulator) shift; cd /app; exec /app/daedalus "$@" ;;
    *) exec "$@" ;;
esac
