#!/bin/bash
# First-install operations only. The adjacent scope helper validates path and ownership evidence;
# destructive commands run as the agent, not as the operator. Use the role for the dated-word gate.
set -u
umask 077
for v in KITCHEN_NAME KITCHEN_ACCOUNT KITCHEN_UID KITCHEN_OPERATOR_UID KITCHEN_ACCOUNT_HOME KITCHEN_RUNTIME_DIR KITCHEN_ROOT KITCHEN_UNIT_DIR KITCHEN_SAVE KITCHEN_SYSTEMCTL; do
  [ -n "${!v:-}" ] || { echo "first: REFUSED, nothing changed: $v is not set"; exit 60; }
done
NAME=$KITCHEN_NAME; H=$KITCHEN_ACCOUNT_HOME; X=$KITCHEN_RUNTIME_DIR
R=$KITCHEN_ROOT; UD=$KITCHEN_UNIT_DIR; U=connectome-$NAME.service
SAVE=$KITCHEN_SAVE; STATE=${KITCHEN_STATE_DIR:-}
read -r -a AS <<<"${KITCHEN_AS_ACCOUNT:-}"
MODE=${1:-}; BUILD=${2:-}
HEX='^[a-f0-9]{64}$'
USAGE="usage: kitchen-first.sh hold BUILD|nothing|runs BUILD|mark BUILD WORD WORD|held BUILD|claim BUILD|claim-initial BUILD|scope|deactivate BUILD|remove BUILD"

say() { printf 'first: %s\n' "$*"; }
refuse() { say "REFUSED, nothing changed: $*"; exit 60; }
t() { "${AS[@]}" /usr/bin/env -i HOME=$H USER=$KITCHEN_ACCOUNT LOGNAME=$KITCHEN_ACCOUNT PATH=/usr/bin:/bin LANG=C.UTF-8 XDG_RUNTIME_DIR=$X DBUS_SESSION_BUS_ADDRESS=unix:path=$X/bus "$@"; }
sc() { t $KITCHEN_SYSTEMCTL --user "$@"; }
prop() { sc show -p "$1" --value $U 2>/dev/null; }

case "$MODE" in
  nothing|scope) [ $# -eq 1 ] || refuse "$USAGE" ;;
  hold|runs|held|claim|claim-initial|deactivate|remove) [ $# -eq 2 ] && [[ $BUILD =~ $HEX ]] || refuse "$USAGE" ;;
  mark) [ $# -eq 4 ] && [[ $BUILD =~ $HEX ]] && [[ $3 =~ ^[A-Za-z-]+$ ]] && [[ $4 =~ ^[A-Za-z-]+$ ]] || refuse "$USAGE" ;;
  *) refuse "$USAGE" ;;
esac
[[ $NAME =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || refuse "KITCHEN_NAME is not the name of a deployment"
[ "$(id -u)" = "$KITCHEN_OPERATOR_UID" ] || refuse "run it as the operator (uid $KITCHEN_OPERATOR_UID)"
[ "$(id -u "$KITCHEN_ACCOUNT" 2>/dev/null)" = "$KITCHEN_UID" ] || refuse "$KITCHEN_ACCOUNT is not uid $KITCHEN_UID on this host"
cleanup_started=false
incomplete() { say "$*. Cleanup is incomplete; correct the cause and retry. Exit 62."; exit 62; }
check() {
  local reply
  reply=$(/usr/bin/python3 -I "${BASH_SOURCE[0]%/*}/kitchen-first-scope.py" "$1" "$BUILD") || {
    if $cleanup_started; then incomplete "VALIDATION FAILED: $reply"; fi
    refuse "$reply"
  }
  CHECK=$reply
}
check scope
[ "$MODE" != scope ] || exit 0
[ -n "$(prop LoadState)" ] || refuse "the user manager of $KITCHEN_ACCOUNT does not answer at $X"

MARKER=$SAVE/first-install
there() { [ -e "$1" ] || [ -L "$1" ]; }
# One of the four things an engine installs is there.
installed() { there $R/current || there $UD/$U || there $UD/default.target.wants/$U || there $UD/$U.d; }
# The unit has no process and is not on its way to one.
down() { case "$(prop ActiveState)" in inactive|failed) return 0 ;; esac; return 1; }
links_at() {
  [ "$(readlink $R/current)" = $R/builds/$1 ] && [ "$(readlink $UD/$U)" = $R/builds/$1/managed-units/$U ] && [ "$(readlink $UD/default.target.wants/$U)" = $R/builds/$1/managed-units/$U ]
}
unit_file_of() {
  local f; f=$(prop FragmentPath)
  [ "$f" = "$1/managed-units/$U" ] || { [ "$f" = "$UD/$U" ] && [ "$(readlink $UD/$U)" = "$1/managed-units/$U" ]; }
}
runs() {
  [ "$(prop ActiveState)" = active ] && [ "$(prop SubState)" = running ] && unit_file_of $R/builds/$1 || return 1
  case "$(prop EnvironmentFiles)" in "$R/builds/$1/secrets.env"*) return 0 ;; esac
  return 1
}
word() { if there "$1"; then if [ -L "$1" ]; then echo "-> $(readlink "$1")"; else echo "there"; fi; else echo "not there"; fi; }
where() {
  say "current: $(word $R/current)"
  say "unit link: $(word $UD/$U)"
  say "enable link: $(word $UD/default.target.wants/$U)"
  say "drop-in directory: $(word $UD/$U.d)"
  say "builds: $(if [ -d $R/builds ]; then ls -A $R/builds | tr '\n' ' '; else echo 'no directory'; fi)"
  say "unit: LoadState=$(prop LoadState) ActiveState=$(prop ActiveState) SubState=$(prop SubState) MainPID=$(prop MainPID) NRestarts=$(prop NRestarts)"
}


if [ "$MODE" = nothing ]; then
  where
  if ! installed && down; then say "nothing of $NAME is installed and its unit is not running"; exit 0; fi
  say "something of $NAME is installed, or its unit is running"
  exit 1
fi

if [ "$MODE" = runs ]; then
  where
  if links_at $BUILD && runs $BUILD; then
    say "the agent runs build ${BUILD:0:8}"
    echo "process $(prop MainPID) $(prop NRestarts)"
    exit 0
  fi
  say "the agent does not run build ${BUILD:0:8}"
  exit 1
fi

if [ "$MODE" = hold ]; then
  installed && { where; refuse "something of $NAME is installed already: this is not a first install"; }
  down && [ -z "$(prop FragmentPath)" ] || refuse "the unit $U is not down and unloaded: this is not a first install"
  if there "$MARKER"; then
    check held
    kept="already held"
  else
    check fresh
    mkdir -p -m 700 -- "$SAVE" || refuse "cannot make the private save directory"
    t mkdir -p -m 700 -- "$R" || refuse "cannot make the deployment directory"
    check create
    kept="held"
  fi
  where
  # Names only. A fresh agent starts on a state directory that holds what the operator put there and nothing else.
  if [ -n "$STATE" ] && [ -d "$STATE" ]; then
    say "state directory $STATE: $(ls -A "$STATE" | wc -l) entries ($(ls -A "$STATE" | head -20 | tr '\n' ' '))"
  else
    say "state directory ${STATE:-(not named)}: not there"
  fi
  say "$kept: nothing of $NAME is deployed (no pointer, no unit link, no enable link, no drop-in, the unit down, no build but ${BUILD:0:8}); the marker is $MARKER"
  exit 0
fi

if [ "$MODE" = claim ] || [ "$MODE" = claim-initial ]; then
  check "$MODE"
  exit 0
fi

if [ "$MODE" = mark ]; then
  check held
  sha256sum < $R/state/ownership.json > $SAVE/ownership.prepared.sha256 && printf '%s %s %s\n' "$BUILD" "$3" "$4" > $SAVE/verified || refuse "cannot write in $SAVE"
  say "marked: $(cat $SAVE/verified)"
  exit 0
fi
if [ "$MODE" = held ]; then
  check held
  [ -s $SAVE/verified ] && [ -s $SAVE/ownership.prepared.sha256 ] || refuse "verify has not run for this build"
  read -r marked_build first second < $SAVE/verified
  [ "$marked_build" = "$BUILD" ] || refuse "verify ran for another build"
  if installed || ! down; then where; refuse "something of $NAME is installed, or its unit is running: the first start has been done already, or one failed and left something (remove)"; fi
  [ "$(sha256sum < $R/state/ownership.json)" = "$(cat $SAVE/ownership.prepared.sha256)" ] || refuse "ownership.json changed since verify: something deployed in between; run verify again"
  say "verified: $marked_build $first $second; nothing is installed yet"
  exit 0
fi

# remove or deactivate (the latter always keeps the root and agent state).
grep -Fq -- "$U" /proc/self/cgroup && refuse "this shell is inside $U"
check "$MODE"
if [ "$CHECK" = absent ]; then
  down && [ -z "$(prop FragmentPath)" ] || refuse "no marker and the unit is not down and unloaded"
  say "nothing to remove"
  exit 0
fi
OWNED=${CHECK##*$'\n'}
SOURCE=${CHECK%%$'\n'*}
FRAGMENT=$(prop FragmentPath)
if [ -n "$FRAGMENT" ]; then
  [ "$FRAGMENT" = "$SOURCE" ] || { [ "$FRAGMENT" = "$UD/$U" ] && { [ "$(readlink "$UD/$U")" = "$SOURCE" ] ||
    { [ "$OWNED" = owned-unit ] && ! there "$UD/$U" && down; }; }; } || refuse "the manager loaded a different unit fragment"
else
  down || refuse "the manager did not identify a running unit fragment"
fi
# Recheck after stopping (which may take time) and before recurring into a deletion scope.
# A stop failure must not prevent removing boot links that we know we own.
where
step() {
  cleanup_started=true
  say "step: $*"
  "$@" || incomplete "STEP FAILED: $*"
}
if { [ -n "$FRAGMENT" ] && there "$UD/$U"; } || ! down; then
  cleanup_started=true
  say "step: sc stop $U"; sc stop "$U" 2>/dev/null
fi
check recheck
if there "$UD/default.target.wants/$U"; then step t rm -f -- "$UD/default.target.wants/$U"; fi
if ! down; then
  step sc daemon-reload
  say "STEP FAILED: boot enablement was removed but the manager does not report the unit down. Exit 62."
  exit 62
fi
if there "$UD/$U" || there "$UD/$U.d/90-connectome-env.conf"; then
  step t rm -f -- "$UD/$U" "$UD/$U.d/90-connectome-env.conf"
fi
if [[ "$CHECK" == *$'\nowned-dropin\n'* ]] && [ -d "$UD/$U.d" ] && [ -z "$(ls -A "$UD/$U.d")" ]; then
  step t rmdir -- "$UD/$U.d"
fi
step sc daemon-reload
sc reset-failed "$U" 2>/dev/null
down || { say "STEP FAILED: boot links were removed but the manager does not report the unit down. Exit 62."; exit 62; }
check recheck
if [ "$MODE" = deactivate ]; then
  if there "$R/current"; then step t rm -- "$R/current"; fi
  step rm -f -- "$SAVE/verified" "$SAVE/ownership.prepared.sha256"
  say "deactivated: unit down and boot activation removed; build and agent state retained"
  exit 0
fi

# Record the verified build identity before recursive removal, so a partial rm can be retried even
# if the engine manifest is already gone. The marker is the last thing retired.
check removing
if there "$R/current"; then step t rm -- "$R/current"; fi
check recheck
say "removing the first install of $NAME"
step t rm -rf --one-file-system -- "$R"
if there "$R" || there "$UD/$U" || there "$UD/default.target.wants/$U" || there "$UD/$U.d/90-connectome-env.conf"; then
  say "STEP FAILED: owned entries remain; removal is incomplete. Exit 62."
  exit 62
fi
step rm -f -- "$SAVE/verified" "$SAVE/ownership.prepared.sha256" "$MARKER"
say "removed: the unit is down, its boot activation and its deployment root are gone, and the marker is retired"
