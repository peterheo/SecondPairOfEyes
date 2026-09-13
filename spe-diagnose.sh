#!/bin/bash
# Why is the box still serving v1?  Run:  sudo bash spe-diagnose.sh
# Read-only except for the last section, which only runs if you pass --fix.
set -uo pipefail
EXPECT=5589226

say() { printf '\n== %s\n' "$*"; }

say "1. what systemd actually runs"
for U in spe sos-kernel; do
  echo "--- $U"
  systemctl cat "$U" 2>/dev/null | grep -E "ExecStart|WorkingDirectory|EnvironmentFile|User=" || echo "  (no unit named $U)"
  echo "  active: $(systemctl is-active "$U" 2>/dev/null)  since: $(systemctl show -p ActiveEnterTimestamp --value "$U" 2>/dev/null)"
done

say "2. the file the running process has open"
PID=$(systemctl show -p MainPID --value spe 2>/dev/null)
if [ -n "${PID:-}" ] && [ "$PID" != "0" ]; then
  echo "  pid: $PID"
  tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null; echo
  echo "  cwd: $(readlink -f /proc/$PID/cwd 2>/dev/null)"
  echo "  started: $(ps -o lstart= -p "$PID" 2>/dev/null)"
else
  echo "  spe has no main pid - it is not running"
fi

say "3. every spe.py on this box, newest first"
find / -name spe.py -not -path '*/proc/*' -printf '%T@ %TY-%Tm-%Td %TH:%TM  %10s  %p\n' 2>/dev/null \
  | sort -rn | head -10 | cut -d' ' -f2-

say "4. does each one have the v2 code in it?"
while read -r f; do
  [ -f "$f" ] || continue
  printf '  %-48s schema=%s execute=%s\n' "$f" \
    "$(grep -o 'SCHEMA_VERSION = "[^"]*"' "$f" | head -1 | sed 's/.*"\(.*\)"/\1/' || echo none)" \
    "$(grep -c '_choose_sandbox' "$f" 2>/dev/null)"
done < <(find / -name spe.py -not -path '*/proc/*' 2>/dev/null | head -10)

say "5. git state of every checkout found"
while read -r d; do
  echo "--- $d"
  git -C "$d" rev-parse --short HEAD 2>/dev/null
  git -C "$d" remote -v 2>/dev/null | head -2
  git -C "$d" status --short 2>/dev/null | head -5
  git -C "$d" log --oneline -1 2>/dev/null
done < <(find / -maxdepth 4 -name .git -type d -not -path '*/proc/*' 2>/dev/null | xargs -r -n1 dirname | head -5)

say "6. what the deploy scripts think"
ls -la /opt/spe 2>/dev/null | head -12
cat /opt/spe/.deploy_user 2>/dev/null && echo "  <- .deploy_user"
grep -E "^REVIEW_MODEL|^REVIEW_FALLBACK" /opt/spe/.env 2>/dev/null || echo "  (no /opt/spe/.env or unreadable)"

say "7. what the service is serving right now"
curl -s -m 10 http://127.0.0.1:8400/v1/health | head -c 600; echo

say "EXPECTED: a checkout at $EXPECT, spe.py with schema=spe/2, REVIEW_MODEL=openai/gpt-oss-120b"

if [ "${1:-}" = "--fix" ]; then
  say "FIXING"
  D=$(systemctl show -p WorkingDirectory --value spe 2>/dev/null)
  EXEC=$(systemctl cat spe 2>/dev/null | grep -m1 ExecStart | sed 's/.*ExecStart=//')
  echo "  unit runs: $EXEC"
  echo "  from: ${D:-unset}"
  # the checkout that actually feeds the unit, derived from ExecStart rather than guessed
  SRC=$(echo "$EXEC" | tr ' ' '\n' | grep -m1 'spe\.py$')
  REPO=$(cd "$(dirname "$SRC")/.." 2>/dev/null && pwd)
  echo "  repo inferred from ExecStart: $REPO"
  if [ -d "$REPO/.git" ]; then
    git config --global --add safe.directory "$REPO" 2>/dev/null
    git -C "$REPO" fetch --all --tags --quiet
    git -C "$REPO" reset --hard origin/main
    echo "  now at: $(git -C "$REPO" rev-parse --short HEAD)"
    APP_USER=$(cat "$REPO/.deploy_user" 2>/dev/null || echo opc)
    chown -R "$APP_USER:$APP_USER" "$REPO"
    [ -f "$REPO/.env" ] && chmod 600 "$REPO/.env"
    systemctl restart sos-kernel spe
    sleep 5
    curl -s -m 10 http://127.0.0.1:8400/v1/health | head -c 400; echo
  else
    echo "  !! $REPO is not a git checkout - the unit is running a COPY of spe.py, not the repo."
    echo "     That is why update.sh changed nothing: it updated the repo and the unit never read it."
    echo "     Point ExecStart at the checkout, or copy the new file over:"
    echo "       cp <checkout>/src/spe.py $SRC && systemctl restart spe"
  fi
fi
