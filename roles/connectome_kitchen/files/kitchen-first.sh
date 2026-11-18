#!/bin/bash
# A first install of one deployment: an agent that is installed from nothing. What holds that nothing is deployed, where
# the agent is after its first start, and the way out. Without kitchen. The pointers of a deployment that runs, its save
# and its way back are files/kitchen-pointers.sh; nothing of that file is used or changed here.
#
#   kitchen-first.sh hold BUILD      before anything is prepared: nothing of this deployment is installed and no build but
#                                    BUILD is there. Keeps the marker <save>/first-install. Changes nothing of the account
#   kitchen-first.sh nothing         is nothing installed, and the unit not running? Changes nothing
#   kitchen-first.sh runs BUILD      does the agent run BUILD (64 hex)? Changes nothing. Prints `process <pid> <restarts>`
#   kitchen-first.sh mark BUILD WORD WORD   keep what verify found of BUILD, and the digest of ownership.json, for the start
#   kitchen-first.sh held BUILD      is BUILD the one verify marked, ownership.json still what it was then, and still
#                                    nothing installed?
#   kitchen-first.sh remove [state]  stop the unit and remove what the first install put there. With `state`: the agent's
#                                    state directory too
#
# Everything it needs is in its environment, set by the role (roles/connectome_kitchen/tasks):
#   KITCHEN_NAME KITCHEN_ACCOUNT KITCHEN_UID KITCHEN_OPERATOR_UID KITCHEN_ACCOUNT_HOME KITCHEN_RUNTIME_DIR
#   KITCHEN_ROOT KITCHEN_UNIT_DIR KITCHEN_SAVE KITCHEN_SYSTEMCTL
#   KITCHEN_STATE_DIR    the agent's state directory: listed by `hold` (names only), removed by `remove state` only
#   KITCHEN_AS_ACCOUNT   the words that make a command the account's (`/usr/sbin/runuser -u NAME --`), or empty
#
# "Installed" is the four things the engine puts in place when it starts a deployment: the pointer `current`, the unit
# link, the enable link and the drop-in directory. "Nothing is deployed" is: none of the four is there (a link that names
# nothing is there), the unit is not running, and `builds` has no build but the one this install makes.
#
# The marker is what tells a deployment this role installed from nothing from every other one. `hold` writes it, into a
# save directory that holds no save of a takeover. `remove` works only where it is, and only while the deployment still
# is what a first install leaves: at most one build. An agent that has had a window since (two builds) is not removed
# here, whatever the marker says.
#
# `remove` writes exactly:
#   the unit                                   stopped (the one unit of this deployment)
#   the enable link, the unit link             removed, if they name a build of this deployment
#   the drop-in 90-connectome-env.conf         removed, and its directory if that leaves it empty
#   <root>                                     removed, with everything in it: builds, cook-config, cook-credentials, the
#                                              engine's bookkeeping, the copies of the secret files the engine wrote, and
#                                              the agent's workspace where it lies there (<root>/workspace)
#   <save>/verified, ownership.prepared.sha256 removed: a build must be verified again before it is started
#   with `state` only: the agent's state directory, removed
# It never writes the secrets file, another unit, the log of the account (<home>/kitchen-takeover) or the marker. Without
# `state` the state directory is left as the process left it: it is not the fresh state of before the first start.
#
# Exit status:
#   0   hold: nothing is deployed (the marker is kept). nothing, runs: yes. mark, held: done, yes. remove: nothing of the
#       deployment is installed and the root is gone (also where nothing was there)
#   1   nothing, runs: no
#   60  refused before any change
#   62  remove: a step failed part way; the line above says which. Run it again when the cause is gone
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
USAGE="usage: kitchen-first.sh hold BUILD|nothing|runs BUILD|mark BUILD WORD WORD|held BUILD|remove [state]"

say() { printf 'first: %s\n' "$*"; }
refuse() { say "REFUSED, nothing changed: $*"; exit 60; }
t() { "${AS[@]}" /usr/bin/env -i HOME=$H USER=$KITCHEN_ACCOUNT LOGNAME=$KITCHEN_ACCOUNT PATH=/usr/bin:/bin LANG=C.UTF-8 XDG_RUNTIME_DIR=$X DBUS_SESSION_BUS_ADDRESS=unix:path=$X/bus "$@"; }
sc() { t $KITCHEN_SYSTEMCTL --user "$@"; }
prop() { sc show -p "$1" --value $U 2>/dev/null; }

case "$MODE" in
  nothing) [ $# -eq 1 ] || refuse "$USAGE" ;;
  remove) [ $# -eq 1 ] || { [ $# -eq 2 ] && [ "$2" = state ]; } || refuse "$USAGE" ;;
  hold|runs|held) [ $# -eq 2 ] && [[ $BUILD =~ $HEX ]] || refuse "$USAGE" ;;
  mark) [ $# -eq 4 ] && [[ $BUILD =~ $HEX ]] && [[ $3 =~ ^[A-Za-z-]+$ ]] && [[ $4 =~ ^[A-Za-z-]+$ ]] || refuse "$USAGE" ;;
  *) refuse "$USAGE" ;;
esac
[[ $NAME =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || refuse "KITCHEN_NAME is not the name of a deployment"
# The paths this script looks at, and `remove` removes from: in the home of the account, the root under the name of the
# deployment, and nothing that climbs.
for p in "$R" "$UD" "$STATE"; do
  [ -z "$p" ] || { [[ $p == "$H"/?* ]] && [[ $p != *..* ]] && [[ $p != *//* ]] && [[ $p != */ ]]; } || refuse "$p is not a plain path in the home of $KITCHEN_ACCOUNT"
done
[ "${R##*/}" = "$NAME" ] || refuse "the root $R is not a directory under the name of the deployment"
[ "$(id -u)" = "$KITCHEN_OPERATOR_UID" ] || refuse "run it as the operator (uid $KITCHEN_OPERATOR_UID)"
[ "$(id -u "$KITCHEN_ACCOUNT" 2>/dev/null)" = "$KITCHEN_UID" ] || refuse "$KITCHEN_ACCOUNT is not uid $KITCHEN_UID on this host"
[ -n "$(prop LoadState)" ] || refuse "the user manager of $KITCHEN_ACCOUNT does not answer at $X"

MARKER=$SAVE/first-install
there() { [ -e "$1" ] || [ -L "$1" ]; }
# One of the four things an engine installs is there.
installed() { there $R/current || there $UD/$U || there $UD/default.target.wants/$U || there $UD/$U.d; }
# The unit has no process and is not on its way to one.
down() { case "$(prop ActiveState)" in inactive|failed) return 0 ;; esac; return 1; }
other_builds() { if [ -d $R/builds ]; then ls -A $R/builds | grep -vx -- "${1:-}"; fi; }
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
marked() { [ -s $MARKER ] && [ "$(cat $MARKER)" = "first-install $NAME" ]; }

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
  [ ! -e $SAVE/links ] || refuse "$SAVE holds the save of a takeover: a first install keeps its marker in a directory of its own"
  installed && { where; refuse "something of $NAME is installed already: this is not a first install. A deployment that runs is taken over (connectome_kitchen_live_build); one a first install left is removed first (remove)"; }
  down || { where; refuse "the unit $U is not down: this is not a first install"; }
  [ -z "$(other_builds $BUILD)" ] || { where; refuse "$R/builds holds another build than ${BUILD:0:8}: this is not a first install"; }
  if [ -e $MARKER ]; then
    marked || refuse "$MARKER is not the marker of a first install of $NAME"
    kept="already held"
  else
    mkdir -p -m 700 $SAVE || refuse "cannot make $SAVE"
    [ "$(stat -c %u:%a $SAVE)" = "$KITCHEN_OPERATOR_UID:700" ] || refuse "$SAVE must be the operator's, mode 0700"
    printf 'first-install %s\n' "$NAME" > $MARKER || refuse "cannot write in $SAVE"
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

marked || refuse "$SAVE holds no marker of a first install of $NAME: prepare has not run with connectome_kitchen_first_install, or this deployment was not installed from nothing by this role"

if [ "$MODE" = mark ]; then
  sha256sum < $R/state/ownership.json > $SAVE/ownership.prepared.sha256 && printf '%s %s %s\n' "$BUILD" "$3" "$4" > $SAVE/verified || refuse "cannot write in $SAVE"
  say "marked: $(cat $SAVE/verified)"
  exit 0
fi
if [ "$MODE" = held ]; then
  [ -s $SAVE/verified ] && [ -s $SAVE/ownership.prepared.sha256 ] || refuse "verify has not run for this build"
  read -r marked_build first second < $SAVE/verified
  [ "$marked_build" = "$BUILD" ] || refuse "verify ran for another build"
  if installed || ! down; then where; refuse "something of $NAME is installed, or its unit is running: the first start has been done already, or one failed and left something (remove)"; fi
  [ "$(sha256sum < $R/state/ownership.json)" = "$(cat $SAVE/ownership.prepared.sha256)" ] || refuse "ownership.json changed since verify: something deployed in between; run verify again"
  say "verified: $marked_build $first $second; nothing is installed yet"
  exit 0
fi

# remove
grep -q "$U" /proc/self/cgroup && refuse "this shell is inside $U: the stop would end it half way"
[ "$(other_builds | wc -l)" -le 1 ] || { where; refuse "$R/builds holds more than one build: this deployment has had a window since its first install and is not removed here"; }
for link in $UD/$U $UD/default.target.wants/$U; do
  [ ! -L $link ] || [[ "$(readlink $link)" == "$R"/builds/* ]] || refuse "$link names a file outside this deployment ($(readlink $link)): not a unit this install put there"
  [ ! -e $link ] || [ -L $link ] || refuse "$link is not a link: not a unit this install put there"
done
[ ! -e $UD/$U.d ] || [ -d $UD/$U.d ] || refuse "$UD/$U.d is not a directory"
[ "${2:-}" != state ] || [ -n "$STATE" ] || refuse "no state directory is named"
where
if ! installed && down && ! there $R && { [ "${2:-}" != state ] || ! there "$STATE"; }; then
  rm -f $SAVE/verified $SAVE/ownership.prepared.sha256
  say "nothing of $NAME is installed and $R is not there: nothing to remove. Nothing changed, nothing stopped."
  exit 0
fi
step() { say "step: $*"; "$@" || { say "STEP FAILED (status $?): $*"; where; say "The removal is not complete; run it again when the cause is gone. Exit 62."; exit 62; }; }
say "removing the first install of $NAME: its unit is stopped; the secrets file is not touched$([ "${2:-}" = state ] || echo ', and neither is the state directory')"
# A unit the manager does not know cannot be stopped, and need not be: what counts is that it is down afterwards.
say "step: sc stop $U"; sc stop $U 2>/dev/null
down || { say "STEP FAILED: after the stop the unit is not down"; where; say "The removal is not complete. Exit 62."; exit 62; }
step t rm -f -- $UD/default.target.wants/$U $UD/$U $UD/$U.d/90-connectome-env.conf
if [ -d $UD/$U.d ]; then
  if [ -z "$(ls -A $UD/$U.d)" ]; then step t rmdir $UD/$U.d; else say "note: $UD/$U.d holds files this install did not put there ($(ls -A $UD/$U.d | tr '\n' ' ')): they and the directory are left"; fi
fi
step sc daemon-reload
sc reset-failed $U 2>/dev/null
step t rm -rf -- $R
[ "${2:-}" != state ] || step t rm -rf -- "$STATE"
rm -f $SAVE/verified $SAVE/ownership.prepared.sha256
# Done is what is there, not what a step said.
if there $R/current || there $UD/$U || there $UD/default.target.wants/$U || there $UD/$U.d/90-connectome-env.conf || there $R || ! down || { [ "${2:-}" = state ] && there "$STATE"; }; then
  say "STEP FAILED: after the steps something of the deployment is still there"; where; say "The removal is not complete. Exit 62."; exit 62
fi
where
say "removed: the unit of $NAME is down, its links and its drop-in are gone, and so is $R. $(if [ "${2:-}" = state ]; then echo "The state directory $STATE is gone too."; else echo "The state directory ${STATE:-} is left as the process left it."; fi) The secrets file and the log of the account are untouched; the marker stays."
exit 0
