#!/usr/bin/env bash
# Manage the production VM's ~/aloud/.env from a dev machine.
#
#   bash scripts/sync-env.sh                    copy the local .env to the VM
#   bash scripts/sync-env.sh --list             show the saved versions
#   bash scripts/sync-env.sh --restore <commit> put back the .env that ran
#                                               with <commit> (for a rollback)
#
# Versions stay on the VM, named after the commit they were in production
# with. The VM's checkout is hard-reset to the deployed commit by every
# deploy, so its HEAD says what is running. Whenever this script is about
# to replace .env, it first saves the current one as .env.<commit>, where
# <commit> is that HEAD. An existing .env.<commit> is never overwritten (a
# second, different save for the same commit becomes .env.<commit>-2).
#
# A saved file covers every commit deployed since the save before it, up
# to its own commit: if the env was synced while A was live, then B and C
# were deployed, and the next sync happened under C, .env.C is what B ran
# with too. --restore <commit> works that out. Commits newer than every
# saved file ran with the .env that is on the VM now.
#
# Copying (the default):
#   1. uploads the local .env beside the VM's (as .env.incoming);
#   2. on the VM, builds the file that would replace .env and prints a
#      report of key NAMES only: added, changed, kept, removed;
#   3. asks before replacing.
# The local file wins, and a key that is only on the VM is removed (the
# report lists it first). One protection, because a plain copy would take
# production down: keys in KEEP_ON_VM keep the VM's value whether or not
# the local file has them (the database password, the domain Caddy serves,
# and the log level: DEBUG logs transcript text, which production must not).
#
# It never prints a value, and it does not restart anything: the running
# backend keeps its old environment until the next deploy or restart. Sync
# (or restore) right before the deploy it is for (deployment.md, section
# 13), so that code is never restarted under the wrong file. The tags are
# only right if .env is changed through this script, not by hand.
#
# Needs the gcloud CLI, signed in, with the project set (deployment.md).
set -euo pipefail

VM="${ALOUD_VM:-aloud}"
ZONE="${ALOUD_ZONE:-us-west1-b}"
KEEP_ON_VM="POSTGRES_PASSWORD ALOUD_DOMAIN ALOUD_EMAIL LOG_LEVEL"

# ---------------------------------------------------------------- on the VM
keys() { grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$1" | tr -d '=' | sort -u; }
line() { grep -E "^$2=" "$1" | tail -n 1; }
describe() { git log -1 --format='%h %s' "$1" 2>/dev/null | cut -c1-70; }

deployed() {  # the commit running in production: the VM checkout's HEAD
  git rev-parse --short=7 HEAD 2>/dev/null || echo "unknown-$(date +%Y%m%d-%H%M%S)"
}

saved() {  # saved versions, oldest commit first: "<commit time> <file>"
  local f tag
  for f in .env.[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*; do
    [ -f "$f" ] || continue
    tag="${f#.env.}"
    echo "$(git log -1 --format=%ct "${tag%%-*}" 2>/dev/null || echo 0) $f"
  done | sort -n
}

save_current() {  # .env -> .env.<commit it was in production with>
  [ -f .env ] || return 0
  local tag name n=2
  tag="$(deployed)"
  name=".env.$tag"
  while [ -f "$name" ]; do
    if cmp -s .env "$name"; then
      echo "The current .env is already saved as ~/aloud/$name."
      return 0
    fi
    name=".env.$tag-$n"
    n=$((n + 1))
  done
  cp -p .env "$name"
  chmod 600 "$name"
  echo "Saved the current .env as ~/aloud/$name (it ran with $(describe "$tag"))."
}

remote_plan() {
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
    removed=" $(comm -13 <(keys "$next") <(keys "$cur") | tr '\n' ' ')"
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
  [ -n "${removed// /}" ] || removed=""
  echo "In production on $(hostname) now: $(describe HEAD)"
  echo "Planned ~/aloud/.env (key names only):"
  echo "  added:              ${added:- none}"
  echo "  value changes:      ${changed:- none}"
  echo "  kept the VM's own:  ${kept:- none}"
  echo "  REMOVED (only on the VM, not in the local file): ${removed:- none}"
  [ ! -f "$cur" ] || echo "The current .env would first be saved as .env.$(deployed)."
}

remote_apply() {
  [ -f .env.next ] || { echo "nothing planned: run the script again"; exit 1; }
  save_current
  mv .env.next .env
  chmod 600 .env
  echo "Replaced ~/aloud/.env. Nothing was restarted."
}

remote_discard() { rm -f .env.next .env.incoming; }

remote_list() {
  echo "In production on $(hostname) now: $(describe HEAD)  (uses ~/aloud/.env)"
  local found="" t f tag
  while read -r t f; do
    [ -n "$f" ] || continue
    found=1
    tag="${f#.env.}"
    echo "  $f   saved $(date -r "$f" +%Y-%m-%d)   ran with $(describe "${tag%%-*}")"
  done < <(saved)
  [ -n "$found" ] || echo "  no saved versions yet (one is saved each time .env is replaced)"
}

pick_for() {  # the saved file that was in use when commit $1 was deployed
  local t f tag
  while read -r t f; do
    tag="${f#.env.}"
    case "$tag" in *-*) continue ;; esac   # later saves for the same commit
    if git merge-base --is-ancestor "$1" "$tag" 2>/dev/null; then
      echo "$f"
      return 0
    fi
  done < <(saved)
}

remote_restore() {  # $1 = commit, $2 = "apply" to do it
  local want file
  want="$(git rev-parse --verify --quiet "$1^{commit}")" || {
    echo "The VM's checkout does not know commit $1 (it only has what deploys fetched)."
    exit 1
  }
  file="$(pick_for "$want")"
  if [ -z "$file" ]; then
    echo "No saved version covers $(describe "$want")."
    echo "The .env on the VM now is the one that commit ran with: nothing to restore."
    exit 3
  fi
  if cmp -s "$file" .env; then
    echo "~/aloud/.env is already the version that ran with $(describe "$want") ($file)."
    exit 3
  fi
  if [ "${2:-}" != "apply" ]; then
    echo "In production on $(hostname) now: $(describe HEAD)"
    echo "For $(describe "$want") the .env to put back is ~/aloud/$file."
    echo "The current .env would first be saved as .env.$(deployed)."
    return 0
  fi
  save_current
  cp -p "$file" .env
  chmod 600 .env
  echo "~/aloud/.env is now $file. Nothing was restarted."
}

case "${1:-}" in
  --remote-*)
    cd "$HOME/aloud"
    umask 077
    case "$1" in
      --remote-plan) remote_plan ;;
      --remote-apply) remote_apply ;;
      --remote-discard) remote_discard ;;
      --remote-list) remote_list ;;
      --remote-restore) remote_restore "$2" "${3:-}" ;;
      *) echo "unknown: $1"; exit 2 ;;
    esac
    exit 0
    ;;
esac

# ------------------------------------------------------------- on this machine
mode="sync"
commit=""
case "${1:-}" in
  "") ;;
  --list) mode="list" ;;
  --restore)
    mode="restore"
    commit="${2:-}"
    # also keeps anything but a hash out of the command run on the VM
    [[ "$commit" =~ ^[0-9a-fA-F]{7,40}$ ]] || { echo "usage: sync-env.sh --restore <commit hash>"; exit 2; }
    ;;
  *) echo "usage: sync-env.sh [--list | --restore <commit hash>]"; exit 2 ;;
esac

cd "$(dirname "$0")/.."
command -v gcloud >/dev/null || { echo "gcloud CLI not found (deployment.md, section 2)."; exit 1; }
[ "$mode" != "sync" ] || [ -f .env ] || { echo "No .env in $(pwd)."; exit 1; }

# Git Bash would rewrite "vm:path" arguments as Windows paths.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

on_vm() {  # run this same script on the VM, tolerant of CRLF line endings
  gcloud compute ssh "$VM" --zone="$ZONE" --tunnel-through-iap \
    --command="tr -d '\r' < aloud/.sync-env.sh > aloud/.sync-env.lf && bash aloud/.sync-env.lf $*"
}
cleanup() {
  gcloud compute ssh "$VM" --zone="$ZONE" --tunnel-through-iap \
    --command="rm -f aloud/.sync-env.sh aloud/.sync-env.lf" >/dev/null 2>&1 || true
}
confirm() {
  local answer
  read -r -p "$1 [y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ]
}

echo "Connecting to $VM ($ZONE)..."
[ "$mode" != "sync" ] || gcloud compute scp --zone="$ZONE" --tunnel-through-iap .env "$VM:aloud/.env.incoming"
gcloud compute scp --zone="$ZONE" --tunnel-through-iap scripts/sync-env.sh "$VM:aloud/.sync-env.sh"
trap cleanup EXIT

case "$mode" in
  list)
    on_vm --remote-list
    ;;
  restore)
    on_vm --remote-restore "$commit" || exit 0   # nothing to restore: already said why
    echo
    if confirm "Put that .env back?"; then
      on_vm --remote-restore "$commit" apply
      echo "Next: run the deploy for that commit, or restart the backend."
    else
      echo "Left the VM's .env as it was."
    fi
    ;;
  sync)
    on_vm --remote-plan
    echo
    if confirm "Replace the VM's .env with this?"; then
      on_vm --remote-apply
      echo "Next: deploy, or restart the backend, for the running app to pick it up."
    else
      on_vm --remote-discard
      echo "Left the VM's .env as it was."
    fi
    ;;
esac
