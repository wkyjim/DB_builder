#!/bin/sh
# Hermes PM container-side SSH wrapper for the DB_builder GitHub deploy key.
#
# Purpose: the read-only mounted private key carries 0777 drvfs permissions
# which OpenSSH refuses. This wrapper transparently maintains a 0600 runtime
# copy at /var/tmp (tmpfs, RAM-backed, never synced to OneDrive or the repo)
# and execs ssh with strict host checking.
#
# Invocation: exported as GIT_SSH_COMMAND by the Hermes PM container session
# BEFORE running git push/fetch/ls-remote. Environment variables override
# repo-local core.sshCommand, so the Windows host Git usage of this same
# repository (which shares .git via OneDrive) is unaffected.
#
# This file contains NO secrets and is safe to commit.
set -eu

RUNTIME_KEY="/var/tmp/hermes-db-builder-deploy"
MOUNTED_KEY="/run/secrets/hermes-github/db_builder_deploy"
KNOWN_HOSTS="/run/secrets/hermes-github/github_known_hosts"

# Self-heal: recreate the runtime copy if missing (e.g. after restart),
# and enforce 0600 on EVERY invocation (not only on first copy).
if [ ! -f "$RUNTIME_KEY" ]; then
    if [ ! -r "$MOUNTED_KEY" ]; then
        echo "hermes-git-ssh: mounted key not readable at $MOUNTED_KEY" >&2
        exit 1
    fi
    cp "$MOUNTED_KEY" "$RUNTIME_KEY" || exit 1
fi
chmod 600 "$RUNTIME_KEY" || exit 1

exec ssh -i "$RUNTIME_KEY" \
    -o IdentitiesOnly=yes \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    -o StrictHostKeyChecking=yes \
    "$@"
