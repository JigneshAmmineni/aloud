#!/usr/bin/env bash
# Copy the local .env to the production VM's ~/aloud/.env.
#
#   bash scripts/sync-env.sh
#
# What it does:
#   1. uploads the local .env beside the VM's (as .env.incoming);
#   2. on the VM, builds the file that would replace .env and prints a
#      report of key NAMES only: added, changed, kept, removed;
#   3. asks before replacing; the old file stays as .env.bak-<timestamp>.
#
# It is a replace: the local file wins, and a key that is only on the VM
# is removed (the report lists it first). One protection, because a plain
# copy would take production down: keys in KEEP_ON_VM keep the VM's value
# whether or not the local file has them (the database password, the
# domain Caddy serves, and the log level: DEBUG logs transcript text,
# which production must not).
#
# It never prints a value, and it does not restart anything: the running
# backend keeps its old environment until the next deploy or restart.
# Sync right before a deploy (deployment.md, section 13) so that old code
# is never restarted under the new file.
#
# Needs the gcloud CLI, signed in, with the project set (deployment.md).
set -euo pipefail

VM="${ALOUD_VM:-aloud}"
ZONE="${ALOUD_ZONE:-us-west1-b}"
KEEP_ON_VM="POSTGRES_PASSWORD ALOUD_DOMAIN ALOUD_EMAIL LOG_LEVEL"

# ---------------------------------------------------------------- on the VM
keys() { grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$1" | tr -d '=' | sort -u; }
line() { grep -E "^$2=" "$1" | tail -n 1; }

remote_plan() {
  cd "$HOME/aloud"
  umask 077
  local cur=.env incoming=.env.incoming next=.env.next
  [ -f "$incoming" ] || { echo "no $incoming on the VM: upload failed"; exit 1; }
  tr -d '\r' < "$incoming" > "$next"          # a Windows file has CRLF endings
  [ -z "$(tail -c 1 "$next")" ] || echo >> "$next"
  local kept="" removed="" added="" changed=""
  if [ -f "$cur" ]; then
    for k in $KEEP_ON_VM; do
      if grep -qE "^$k=" "$cur"; then
        { grep -vE "^$k=" "$next" || true; } > "$next.tmp"
        mv "$next.tmp" "$next"
        line "$cur" "$k" >> "$next"
        kept="$kept $k"
      fi
    done
    removed=" $(comm -13 <(keys "$next") <(keys "$cur") | tr '
' ' ')"
    for k in $(keys "$next"); do
      if ! grep -qE "^$k=" "$cur"; then
        added="$added $k"
      elif [ "$(line "$next" "$k")" != "$(line "$cur" "$k")" ]; then
        changed="$changed $k"
      fi
    done
  else
    added=" $(keys "$next" | tr '\n' ' ')"
  fi
  chmod 600 "$next"
  rm -f "$incoming"
  echo "Planned ~/aloud/.env on $(hostname) (key names only):"
  echo "  added:              ${added:- none}"
  echo "  value changes:      ${changed:- none}"
  echo "  kept the VM's own:  ${kept:- none}"
  [ -n "${removed// /}" ] || removed=""
  echo "  REMOVED (only on the VM, not in the local file): ${removed:- none}"
}

remote_apply() {
  cd "$HOME/aloud"
  umask 077
  [ -f .env.next ] || { echo "nothing planned: run the script again"; exit 1; }
  if [ -f .env ]; then
    backup=".env.bak-$(date +%Y%m%d-%H%M%S)"
    cp -p .env "$backup"
    echo "Old file saved as ~/aloud/$backup"
  fi
  mv .env.next .env
  chmod 600 .env
  echo "Replaced ~/aloud/.env. Nothing was restarted."
}

remote_discard() { rm -f "$HOME/aloud/.env.next" "$HOME/aloud/.env.incoming"; }

case "${1:-}" in
  --remote-plan) remote_plan; exit 0 ;;
  --remote-apply) remote_apply; exit 0 ;;
  --remote-discard) remote_discard; exit 0 ;;
esac

# ------------------------------------------------------------- on this machine
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env in $(pwd)."; exit 1; }
command -v gcloud >/dev/null || { echo "gcloud CLI not found (deployment.md, section 2)."; exit 1; }

# Git Bash would rewrite "vm:path" arguments as Windows paths.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

on_vm() {  # run this same script on the VM, tolerant of CRLF line endings
  gcloud compute ssh "$VM" --zone="$ZONE" --tunnel-through-iap \
    --command="tr -d '\r' < aloud/.sync-env.sh > aloud/.sync-env.lf && bash aloud/.sync-env.lf $1"
}
cleanup() {
  gcloud compute ssh "$VM" --zone="$ZONE" --tunnel-through-iap \
    --command="rm -f aloud/.sync-env.sh aloud/.sync-env.lf" >/dev/null 2>&1 || true
}

echo "Uploading .env to $VM ($ZONE)..."
gcloud compute scp --zone="$ZONE" --tunnel-through-iap .env "$VM:aloud/.env.incoming"
gcloud compute scp --zone="$ZONE" --tunnel-through-iap scripts/sync-env.sh "$VM:aloud/.sync-env.sh"
trap cleanup EXIT

on_vm --remote-plan
echo
read -r -p "Replace the VM's .env with this? [y/N] " answer
if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
  on_vm --remote-apply
  echo "Next: deploy, or restart the backend, for the running app to pick it up."
else
  on_vm --remote-discard
  echo "Left the VM's .env as it was."
fi
