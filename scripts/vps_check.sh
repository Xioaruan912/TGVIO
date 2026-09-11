#!/bin/sh
set -eu

: "${VPS_HOST:=199.47.242.40}"
: "${VPS_PORT:=22}"
: "${VPS_USER:=root}"
: "${VPS_SSH_KEY:=/root/.ssh/id_ed25519}"

exec ssh \
  -i "$VPS_SSH_KEY" \
  -p "$VPS_PORT" \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=no \
  "$VPS_USER@$VPS_HOST" \
  'hostname; df -h /; docker version --format "{{.Server.Version}}"'

