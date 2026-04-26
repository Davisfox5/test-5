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

exec gosu linda "$@"
