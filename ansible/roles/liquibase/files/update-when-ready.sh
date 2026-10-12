#!/bin/sh
# Managed by Ansible (role: liquibase).
# Applies the pending changesets of one project:  update-when-ready.sh PROJECT [WAIT_SECONDS]
#
# With WAIT_SECONDS it first waits, for at most that long, until the database accepts the project's login.
# On a new lab it is started while the database server is still being installed, and begins the moment
# the database is there.
project="$1"
wait="${2:-0}"
liquibase="/usr/local/bin/liquibase-$project"

if [ "$wait" -gt 0 ]; then
    deadline=$(( $(date +%s) + wait ))
    while :; do
        out=$("$liquibase" status 2>&1) && break
        case "$out" in
            *"Connection could not be created"*) ;; # no server, no database or no login yet: wait
            *) break ;;                             # anything else is for `update` to report
        esac
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "The database of $project accepted no login for $wait s. The last attempt said:" >&2
            printf '%s\n' "$out" >&2
            exit 1
        fi
        sleep 5
    done
fi
exec "$liquibase" update
