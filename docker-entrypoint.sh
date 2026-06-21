#!/bin/sh
# LINDA container entrypoint.
#
# Fly volumes mount as root:root regardless of in-image ownership. We
# run the app as the non-root `linda` user, so any process that needs to
# write to a mounted volume (today: beat → /data/celerybeat-schedule)
# would otherwise hit EACCES.
#
# Strategy: stay PID 1 as root just long enough to chown known mount
# points, then drop to linda via gosu. exec replaces this shell so
# signals propagate cleanly to the real CMD.

set -e

if [ -d /data ]; then
    chown -R linda:linda /data
fi

# Prometheus multiprocess dir. Every uvicorn worker / celery prefork child
# records metrics into its own file here; the /metrics endpoint aggregates
# them. Must exist and be writable by `linda` BEFORE the app imports
# prometheus_client (it reads PROMETHEUS_MULTIPROC_DIR at metric-creation
# time). Wipe on boot so a prior run's dead-pid files don't linger.
PROM_DIR="${PROMETHEUS_MULTIPROC_DIR:-/tmp/prometheus_multiproc}"
rm -rf "$PROM_DIR"
mkdir -p "$PROM_DIR"
chown -R linda:linda "$PROM_DIR"

exec gosu linda "$@"
