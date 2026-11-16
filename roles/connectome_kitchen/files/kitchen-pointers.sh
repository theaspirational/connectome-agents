#!/bin/bash
# The pointers of one deployment: what names the build an agent runs, saved, looked at, and put back. Without kitchen.
#
#   kitchen-pointers.sh save          keep what `back` needs, before anything is prepared. Changes nothing of the deployment
#   kitchen-pointers.sh look          does the agent run the saved build? Changes nothing
#   kitchen-pointers.sh runs BUILD    does the agent run BUILD (64 hex)? Changes nothing. Prints `process <pid> <restarts>`
#   kitchen-pointers.sh back          put code and configuration back to the saved build and start the agent on it
#   kitchen-pointers.sh mark BUILD WORD WORD   keep what verify found of BUILD, and the digest of ownership.json, for the start
#   kitchen-pointers.sh held BUILD    is BUILD the one verify marked, and ownership.json still what it was then?
#   kitchen-pointers.sh clear [NAME...]   after a way back: remove the secret files NAME that came after the save
#
# Everything it needs is in its environment, set by the role (roles/connectome_kitchen/tasks):
#   KITCHEN_NAME KITCHEN_ACCOUNT KITCHEN_UID KITCHEN_OPERATOR_UID KITCHEN_ACCOUNT_HOME KITCHEN_RUNTIME_DIR
#   KITCHEN_ROOT KITCHEN_UNIT_DIR KITCHEN_SAVE KITCHEN_OLD KITCHEN_SYSTEMCTL
#   KITCHEN_AS_ACCOUNT   the words that make a command the account's (`/usr/sbin/runuser -u NAME --`), or empty
#
# Why this is not the engine: a kitchen names a build by its adapter contract, and the build an agent runs before a
# contract change has the old contract's name. The engine of the new contract cannot ask for that build, so it cannot
# switch back to it once its own start has succeeded. (While its start is failing it does put the previous build back
# itself.) This script moves the same five things the engine moves, to where `save` found them.
#
# `back` is CODE AND CONFIGURATION ONLY. It writes exactly:
#   current, the unit link, the enable link   -> the saved build
#   the drop-in 90-connectome-env.conf        <- the saved copy
#   <root>/state/ownership.json               <- the saved copy (the engine's list of its files: paths and digests)
# It does not write the agent's state directory, the secrets file, <root>/state/secret-files, a build directory,
# cook-config or cook-credentials. Nothing old is restored into the agent's state: the old code starts on the state as
# the new build left it. It stops and starts the unit: one interruption.
#
# `back` is done when all five are the saved ones AND the agent runs the saved build. While it is putting them back it
# keeps <save>/back.incomplete. A `back` that failed part way leaves that file, and the next `back` puts all five back
# again, whatever the links already say, before it says anything is done. That file is what tells "a way back that did
# not finish" from "the new build was prepared and never started": there the links and the drop-in are the saved ones
# too, ownership.json rightly holds what prepare added, and nothing is put back.
#
# `clear` is for the retry after a way back. A start writes the secret files the request adds; the way back leaves them,
# and the saved deployment does not declare them, so the engine refuses to verify or start beside them. `clear` removes
# NAME from <root>/state/secret-files only if all of this holds: the way back is complete (all five saved ones, no
# back.incomplete) and the agent runs the saved build; NAME was not there when `save` listed that directory; the
# environment file of the saved build has no line for NAME; and NAME is a regular file. Nothing else is removed.
#
# Exit status:
#   0   save: saved (or already saved, the same). look, runs: yes. back: the agent runs the saved build; if it already
#       did, nothing was changed and nothing restarted
#   1   look, runs: no
#   60  refused before any change
#   61  back: everything was put back but the agent is not active and running on the saved build. IT MAY BE DOWN
#   62  back: a step failed part way; the line above says which. IT MAY BE DOWN
#   63  clear: a removal failed. The agent is not touched by clear
set -u
umask 077
for v in KITCHEN_NAME KITCHEN_ACCOUNT KITCHEN_UID KITCHEN_OPERATOR_UID KITCHEN_ACCOUNT_HOME KITCHEN_RUNTIME_DIR KITCHEN_ROOT KITCHEN_UNIT_DIR KITCHEN_SAVE KITCHEN_OLD KITCHEN_SYSTEMCTL; do
  [ -n "${!v:-}" ] || { echo "pointers: REFUSED, nothing changed: $v is not set"; exit 60; }
done
NAME=$KITCHEN_NAME; H=$KITCHEN_ACCOUNT_HOME; X=$KITCHEN_RUNTIME_DIR
R=$KITCHEN_ROOT; UD=$KITCHEN_UNIT_DIR; U=connectome-$NAME.service
SAVE=$KITCHEN_SAVE; OLD=$KITCHEN_OLD
read -r -a AS <<<"${KITCHEN_AS_ACCOUNT:-}"
WAIT=${KITCHEN_BACK_WAIT:-60}; SETTLE=${KITCHEN_BACK_SETTLE:-10}
MODE=${1:-}; BUILD=${2:-}
HEX='^[a-f0-9]{64}$'

say() { printf 'pointers: %s\n' "$*"; }
refuse() { say "REFUSED, nothing changed: $*"; exit 60; }
t() { "${AS[@]}" /usr/bin/env -i HOME=$H USER=$KITCHEN_ACCOUNT LOGNAME=$KITCHEN_ACCOUNT PATH=/usr/bin:/bin LANG=C.UTF-8 XDG_RUNTIME_DIR=$X DBUS_SESSION_BUS_ADDRESS=unix:path=$X/bus "$@"; }
sc() { t $KITCHEN_SYSTEMCTL --user "$@"; }
prop() { sc show -p "$1" --value $U 2>/dev/null; }

case "$MODE" in
  save|look|back) [ $# -eq 1 ] || refuse "usage: kitchen-pointers.sh save|look|back|runs BUILD|mark BUILD WORD WORD|held BUILD|clear [NAME...]" ;;
  clear) for n in "${@:2}"; do [[ $n =~ ^[A-Z_][A-Z0-9_]*$ ]] || refuse "usage: kitchen-pointers.sh clear [NAME...], each the environment name of a secret file"; done ;;
  runs|held) [ $# -eq 2 ] && [[ $BUILD =~ $HEX ]] || refuse "usage: kitchen-pointers.sh $MODE BUILD" ;;
  mark) [ $# -eq 4 ] && [[ $BUILD =~ $HEX ]] && [[ $3 =~ ^[A-Za-z-]+$ ]] && [[ $4 =~ ^[A-Za-z-]+$ ]] || refuse "usage: kitchen-pointers.sh mark BUILD WORD WORD" ;;
  *) refuse "usage: kitchen-pointers.sh save|look|back|runs BUILD|mark BUILD WORD WORD|held BUILD|clear [NAME...]" ;;
esac
[[ $OLD =~ $HEX ]] || refuse "KITCHEN_OLD is not a build name"
[ "$(id -u)" = "$KITCHEN_OPERATOR_UID" ] || refuse "run it as the operator (uid $KITCHEN_OPERATOR_UID)"
[ "$(id -u "$KITCHEN_ACCOUNT" 2>/dev/null)" = "$KITCHEN_UID" ] || refuse "$KITCHEN_ACCOUNT is not uid $KITCHEN_UID on this host"
[ -n "$(prop LoadState)" ] || refuse "the user manager of $KITCHEN_ACCOUNT does not answer at $X"

links_at() {
  [ "$(readlink $R/current)" = $R/builds/$1 ] && [ "$(readlink $UD/$U)" = $R/builds/$1/managed-units/$U ] && [ "$(readlink $UD/default.target.wants/$U)" = $R/builds/$1/managed-units/$U ]
}
# The loaded unit file is the one of build directory $1. A manager names it by the link in the unit directory or by
# the link's target; what it has loaded from the drop-in is told by EnvironmentFiles.
unit_file_of() {
  local f; f=$(prop FragmentPath)
  [ "$f" = "$1/managed-units/$U" ] || { [ "$f" = "$UD/$U" ] && [ "$(readlink $UD/$U)" = "$1/managed-units/$U" ]; }
}
runs() {
  [ "$(prop ActiveState)" = active ] && [ "$(prop SubState)" = running ] && unit_file_of $R/builds/$1 || return 1
  case "$(prop EnvironmentFiles)" in "$R/builds/$1/secrets.env"*) return 0 ;; esac
  return 1
}
where() {
  say "current -> $(readlink $R/current)"
  say "unit link -> $(readlink $UD/$U)"
  say "enable link -> $(readlink $UD/default.target.wants/$U)"
  say "unit: ActiveState=$(prop ActiveState) SubState=$(prop SubState) MainPID=$(prop MainPID) NRestarts=$(prop NRestarts) started=$(prop ExecMainStartTimestamp)"
  say "unit file: $(prop FragmentPath)"
  say "environment file: $(prop EnvironmentFiles)"
}

if [ "$MODE" = mark ]; then
  [ -s $SAVE/links ] || refuse "$SAVE holds no save: prepare has not run"
  sha256sum < $R/state/ownership.json > $SAVE/ownership.prepared.sha256 && printf '%s %s %s\n' "$BUILD" "$3" "$4" > $SAVE/verified || refuse "cannot write in $SAVE"
  say "marked: $(cat $SAVE/verified)"
  exit 0
fi
if [ "$MODE" = held ]; then
  [ -s $SAVE/verified ] && [ -s $SAVE/ownership.prepared.sha256 ] || refuse "verify has not run for this build"
  read -r marked first second < $SAVE/verified
  [ "$marked" = "$BUILD" ] || refuse "verify ran for another build"
  [ "$(sha256sum < $R/state/ownership.json)" = "$(cat $SAVE/ownership.prepared.sha256)" ] || refuse "ownership.json changed since verify: something deployed in between; run verify again"
  say "verified: $marked $first $second"
  exit 0
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

if [ "$MODE" = save ]; then
  grep -q "$U" /proc/self/cgroup && say "note: this shell is inside $U; save changes nothing, so it goes on"
  links_at $OLD || { where; refuse "the links do not name build ${OLD:0:8}"; }
  runs $OLD || { where; refuse "the agent does not run build ${OLD:0:8}"; }
  for f in .prepared run.sh managed-units/$U secrets.env; do [ -e $R/builds/$OLD/$f ] || refuse "build ${OLD:0:8} has no $f"; done
  grep -qx "EnvironmentFile=$R/builds/$OLD/secrets.env" $UD/$U.d/90-connectome-env.conf || refuse "the drop-in does not name build ${OLD:0:8}"
  NEW_LINKS=$(printf '%s\n%s\n%s\n' "$(readlink $R/current)" "$(readlink $UD/$U)" "$(readlink $UD/default.target.wants/$U)")
  if [ -e $SAVE/links ]; then
    # A save is never written over: a second `prepare` must find the deployment as the first one saved it.
    [ "$(cat $SAVE/links)" = "$NEW_LINKS" ] && cmp -s $SAVE/90-connectome-env.conf $UD/$U.d/90-connectome-env.conf \
      || refuse "$SAVE holds a save of another state. Move it away only if you know the agent runs what you want to come back to"
    say "already saved in $SAVE, and the links and the drop-in are still the same; ownership.json is kept as first saved"
    where
    exit 0
  fi
  mkdir -p -m 700 $SAVE || refuse "cannot make $SAVE"
  [ "$(stat -c %u:%a $SAVE)" = "$KITCHEN_OPERATOR_UID:700" ] || refuse "$SAVE must be the operator's, mode 0700"
  printf '%s\n' "$NEW_LINKS" > $SAVE/links.new || refuse "cannot write in $SAVE"
  cp $UD/$U.d/90-connectome-env.conf $SAVE/90-connectome-env.conf && cp $R/state/ownership.json $SAVE/ownership.json || refuse "cannot copy the drop-in or ownership.json"
  ls -A $UD/$U.d > $SAVE/dropins; ls -A $R/builds > $SAVE/builds
  # The names of the secret files that are there now (no content): what `clear` may never remove.
  if [ -e $R/state/secret-files ]; then ls -A $R/state/secret-files > $SAVE/secret-files; else : > $SAVE/secret-files; fi || refuse "cannot list $R/state/secret-files"
  mv $SAVE/links.new $SAVE/links
  where | tee $SAVE/where.before
  say "saved in $SAVE: the three links, the drop-in, ownership.json ($(sha256sum < $SAVE/ownership.json | cut -c1-64)), the names of $(grep -c . $SAVE/secret-files) secret files"
  exit 0
fi

# look, back: from what `save` kept, not from what anyone expects.
for f in links 90-connectome-env.conf ownership.json; do [ -s $SAVE/$f ] || refuse "$SAVE/$f is missing: nothing was saved"; done
L1=$(sed -n 1p $SAVE/links); L2=$(sed -n 2p $SAVE/links); L3=$(sed -n 3p $SAVE/links)
[ "$L1" = $R/builds/$OLD ] && [ "$L2" = $R/builds/$OLD/managed-units/$U ] && [ "$L3" = "$L2" ] || refuse "the saved links do not name build ${OLD:0:8}"
grep -qx "EnvironmentFile=$R/builds/$OLD/secrets.env" $SAVE/90-connectome-env.conf || refuse "the saved drop-in does not name build ${OLD:0:8}"
for f in .prepared run.sh managed-units/$U secrets.env; do [ -e $R/builds/$OLD/$f ] || refuse "build ${OLD:0:8} has no $f"; done

files_old() { links_at $OLD && cmp -s $SAVE/90-connectome-env.conf $UD/$U.d/90-connectome-env.conf; }
owned_old() { cmp -s $SAVE/ownership.json $R/state/ownership.json; }
# A way back that began and did not end. Written before its first change, removed after its last check.
INCOMPLETE=$SAVE/back.incomplete
owned_word() { if owned_old; then echo "ownership.json is the saved one"; else echo "ownership.json is not the saved one"; fi; }

if [ "$MODE" = clear ]; then
  [ -e $SAVE/secret-files ] || refuse "$SAVE has no list of the secret files that were there before the window (a save of an earlier role)"
  [ ! -e $INCOMPLETE ] || { where; refuse "a way back did not finish ($INCOMPLETE): run rollback first"; }
  files_old && owned_old && runs $OLD || { where; say "$(owned_word)"; refuse "the agent does not run the saved build ${OLD:0:8} with the links, the drop-in and ownership.json as saved: clear is for after a complete way back"; }
  D=$R/state/secret-files
  for n in "${@:2}"; do
    grep -qxF -- "$n" $SAVE/secret-files && refuse "$n was a secret file before the window (it is in the save): not one this window introduced"
    grep -q -- "^$n=" $R/builds/$OLD/secrets.env && refuse "the environment file of the saved build names $n: the running agent may read that file"
    [ -e $D/$n ] || [ -L $D/$n ] || continue
    [ -f $D/$n ] && [ ! -L $D/$n ] || refuse "$D/$n is not a regular file"
  done
  failed=0
  for n in "${@:2}"; do
    if [ -e $D/$n ]; then
      if t rm -f -- $D/$n && [ ! -e $D/$n ]; then say "removed $D/$n (introduced by this window; the saved deployment does not declare it)"; else say "COULD NOT remove $D/$n"; failed=1; fi
    else
      say "$n: not there, nothing to remove"
    fi
  done
  # As the engine leaves it: no empty directory.
  if [ -d $D ] && [ -z "$(ls -A $D)" ]; then t rmdir $D && say "removed the empty $D" || failed=1; fi
  [ $failed = 0 ] || { say "a removal failed. The agent is not touched by clear. Exit 63."; exit 63; }
  say "secret files there now: $(if [ -d $D ]; then ls -A $D | tr '\n' ' '; fi)(names; before the window: $(tr '\n' ' ' < $SAVE/secret-files))"
  say "cleared: nothing this window introduced is in the way of a retry. Nothing restarted."
  exit 0
fi

if [ -e $INCOMPLETE ]; then
  if files_old && owned_old && runs $OLD; then
    where
    if [ "$MODE" = back ]; then
      rm -f $INCOMPLETE || refuse "cannot remove $INCOMPLETE"
      say "an earlier way back had not finished, and all five things are the saved ones now and the agent runs the saved build ${OLD:0:8}: the way back is complete. Nothing changed, nothing restarted."
    else
      say "the agent runs the saved build ${OLD:0:8} and all five things are the saved ones; $INCOMPLETE is still there: \`back\` would remove it and change nothing else."
    fi
    exit 0
  fi
elif files_old && runs $OLD; then
  where
  say "the agent runs the saved build ${OLD:0:8}; $(owned_word)$(owned_old || echo ' (as after a prepare that was never started, or a start the engine took back itself)'). Nothing to put back; nothing changed, nothing restarted."
  exit 0
fi
if [ "$MODE" = look ]; then
  where
  if [ -e $INCOMPLETE ]; then
    say "AN EARLIER WAY BACK DID NOT FINISH ($(owned_word)). \`back\` would stop the agent, put all five things back and start it."
  else
    say "the agent does not run the saved build ${OLD:0:8}. \`back\` would stop it, put the five things back and start it."
  fi
  exit 1
fi

grep -q "$U" /proc/self/cgroup && refuse "this shell is inside $U: the stop would end it half way"
step() { say "step: $*"; "$@" || { say "STEP FAILED (status $?): $*"; where; say "THE AGENT MAY BE DOWN. Exit 62."; exit 62; }; }
where
if [ ! -e $INCOMPLETE ] && files_old; then
  # No start was made, or the engine took its own start back: the unit is down on the saved build. ownership.json is the
  # engine's own record of that and is left.
  restore=no
  say "the links and the drop-in name the saved build and no way back was left unfinished: only the unit is started; $(owned_word) and is left as it is"
else
  restore=yes
  [ -e $INCOMPLETE ] && say "AN EARLIER WAY BACK DID NOT FINISH ($(owned_word)): all five things are put back again"
  say "putting code and configuration back to build ${OLD:0:8}; the agent's state directory is not touched"
  : >> $INCOMPLETE || refuse "cannot write $INCOMPLETE"
fi
step sc stop $U
if [ $restore = yes ]; then
  step t ln -sfn "$L1" $R/current
  step t ln -sfn "$L2" $UD/$U
  step t ln -sfn "$L3" $UD/default.target.wants/$U
  step install -o $KITCHEN_ACCOUNT -g $KITCHEN_ACCOUNT -m 600 $SAVE/90-connectome-env.conf $UD/$U.d/90-connectome-env.conf
  step install -o $KITCHEN_ACCOUNT -g $KITCHEN_ACCOUNT -m 600 $SAVE/ownership.json $R/state/ownership.json
  # Done is what is there, not what a step said.
  files_old && owned_old || { say "STEP FAILED: after the five steps the links, the drop-in or ownership.json are not the saved ones ($(owned_word))"; where; say "THE AGENT MAY BE DOWN. Exit 62."; exit 62; }
fi
step sc daemon-reload
sc reset-failed $U 2>/dev/null
step sc start $U
waited=0
until runs $OLD; do
  [ $waited -ge $WAIT ] && break
  sleep 2; waited=$((waited + 2))
done
if runs $OLD && files_old && { [ $restore = no ] || owned_old; }; then
  pid=$(prop MainPID)
  sleep $SETTLE
  if runs $OLD && [ "$(prop MainPID)" = "$pid" ]; then
    where
    if [ $restore = yes ]; then
      rm -f $INCOMPLETE || say "note: could not remove $INCOMPLETE; the next rollback finds everything in place and removes it"
      say "the agent runs the saved build ${OLD:0:8}, same process after $SETTLE s. The links, the drop-in and ownership.json are the saved ones. State, auth and offsets are as the new build left them."
    else
      say "the agent runs the saved build ${OLD:0:8}, same process after $SETTLE s. Nothing but the unit was touched."
    fi
    exit 0
  fi
  say "the process changed or stopped within $SETTLE s of the start"
fi
where
say "everything was put back but the agent is NOT active and running on the saved build. IT MAY BE DOWN. Exit 61."
exit 61
