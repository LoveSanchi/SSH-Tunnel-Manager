#!/bin/sh
# SSH_ASKPASS helper used when sshpass is not installed: prints the password from
# the file named by TUNNEL_KEEPER_PASSWORD_FILE (never from argv, so it stays out of ps).
[ -n "${TUNNEL_KEEPER_PASSWORD_FILE:-}" ] || exit 1
exec head -n 1 -- "$TUNNEL_KEEPER_PASSWORD_FILE"
