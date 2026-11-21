#!/usr/bin/env bash
# Single entry point. Needs only bash + docker (+ curl for health waits). Run it from the project root.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
PROJECT_DIR="$(pwd)"

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }
dc()   { docker compose "$@"; }

need_docker() {
  command -v docker >/dev/null 2>&1 || die "docker not found. Install Docker first, then run ./run.sh doctor"
  docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon without sudo. Run ./run.sh doctor and paste the output."
  docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) not available. Run ./run.sh doctor"
}
need_env() { [ -f .env ] || die ".env not found in $PROJECT_DIR. Run: ./run.sh init-env"; }
get_env()  { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-; }
rand_alnum() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-32}"; }

set_env() {  # set_env KEY VALUE  (value passed via environment: no quoting/escaping problems)
  local tmp; tmp="$(mktemp)" || return 1
  K="$1" V="$2" awk 'BEGIN{FS=OFS="="; k=ENVIRON["K"]; v=ENVIRON["V"]; f=0}
       $1==k {print k "=" v; f=1; next} {print} END{if(!f) print k "=" v}' .env > "$tmp" && cat "$tmp" > .env
  rm -f "$tmp"
}

port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
detect_tz() {
  local tz=""
  [ -r /etc/timezone ] && tz="$(head -1 /etc/timezone | tr -d '[:space:]')"
  if [ -z "$tz" ] && command -v timedatectl >/dev/null 2>&1; then tz="$(timedatectl show -p Timezone --value 2>/dev/null)"; fi
  if [ -z "$tz" ] && [ -L /etc/localtime ]; then tz="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"; fi
  echo "${tz:-UTC}"
}
free_port() { local p="$1"; while port_in_use "$p"; do p=$((p+1)); done; echo "$p"; }

# ports that are busy and NOT owned by this project's own containers
port_conflicts() {
  local bad=0 name var p owner
  for var in HOST_PORT_DB HOST_PORT_MAILPIT_SMTP HOST_PORT_MAILPIT_UI HOST_PORT_API HOST_PORT_UI; do
    p="$(get_env "$var")"; [ -n "$p" ] || continue
    if port_in_use "$p"; then
      owner="$(docker ps --filter "publish=$p" --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')"
      case "$owner" in
        agentic_nl_*) ;;  # our own container already holds it
        *) echo "  CONFLICT: host port $p ($var) is in use${owner:+ by container: $owner}"; bad=1 ;;
      esac
    fi
  done
  return $bad
}

cmd_doctor() {
  echo "=== agentic-newsletter doctor ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="
  echo "project dir : $PROJECT_DIR"
  echo "os          : $(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}")"
  echo "arch        : $(uname -m)"
  echo "shell       : $BASH_VERSION"
  echo "timezone    : $(detect_tz)"
  echo "free disk   : $(df -Pk . | awk 'NR==2{printf "%.1f GB", $4/1024/1024}')"
  for t in curl docker; do printf '%-12s: ' "$t"; command -v "$t" || echo "NOT FOUND"; done
  if command -v docker >/dev/null 2>&1; then
    echo "docker      : $(docker --version 2>&1)"
    echo "compose     : $(docker compose version 2>&1 | head -1)"
    if docker info >/dev/null 2>&1; then echo "docker access without sudo: yes"; else echo "docker access without sudo: NO (docker info failed)"; fi
    echo "--- containers publishing ports"
    docker ps --format '  {{.Names}}  {{.Image}}  {{.Ports}}' 2>/dev/null
  fi
  if [ -f .env ]; then
    echo ".env        : present, mode $(stat -c %a .env 2>/dev/null || echo '?') (should be 600)"
    echo "--- project ports (from .env)"
    for var in HOST_PORT_DB HOST_PORT_MAILPIT_SMTP HOST_PORT_MAILPIT_UI HOST_PORT_API HOST_PORT_UI; do
      p="$(get_env "$var")"; if port_in_use "$p"; then s="IN USE"; else s="free"; fi; echo "  $var=$p  $s"
    done
    port_conflicts && echo "port conflicts: none" || echo "port conflicts: see CONFLICT lines above - edit .env or stop the other container"
    for var in POSTGRES_PASSWORD SECRET_KEY SMTP_PASSWORD; do v="$(get_env "$var")"; [ -n "$v" ] && echo "  $var: set (length ${#v})" || echo "  $var: not set"; done
    echo "  EMAIL_MODE=$(get_env EMAIL_MODE) FEED_MODE=$(get_env FEED_MODE) TIMEZONE=$(get_env TIMEZONE)"
  else
    echo ".env        : MISSING -> run ./run.sh init-env"
    echo "--- default ports"
    for p in 55433 1026 8026 8100 8502; do if port_in_use "$p"; then echo "  $p IN USE"; else echo "  $p free"; fi; done
  fi
}

cmd_init_env() {
  if [ -f .env ] && [ "${1:-}" != "--force" ]; then die ".env already exists. Keep it, or run ./run.sh init-env --force to recreate it (this changes the DB password: run ./run.sh reset first)."; fi
  [ -f .env.example ] || die ".env.example missing - is this the project root?"
  cp .env.example .env && chmod 600 .env
  set_env POSTGRES_PASSWORD "$(rand_alnum 32)"
  set_env SECRET_KEY "$(rand_alnum 48)"
  set_env TIMEZONE "$(detect_tz)"
  local db mps mpu api ui
  db="$(free_port 55433)"; mps="$(free_port 1026)"; mpu="$(free_port 8026)"; api="$(free_port 8100)"; ui="$(free_port 8502)"
  set_env HOST_PORT_DB "$db"; set_env HOST_PORT_MAILPIT_SMTP "$mps"; set_env HOST_PORT_MAILPIT_UI "$mpu"; set_env HOST_PORT_API "$api"; set_env HOST_PORT_UI "$ui"
  set_env PUBLIC_BASE_URL "http://localhost:$api"
  echo "created .env (mode 600)"
  echo "  timezone detected : $(get_env TIMEZONE)"
  echo "  ports             : postgres $db, mailpit smtp $mps, mailpit ui $mpu, api $api, web ui $ui"
  echo "  POSTGRES_PASSWORD : set (length $(get_env POSTGRES_PASSWORD | tr -d '\n' | wc -c))"
  echo "  SECRET_KEY        : set (length $(get_env SECRET_KEY | tr -d '\n' | wc -c))"
  if [ -t 0 ]; then
    read -r -p "Configure REAL email sending (SMTP) now? Demo mode needs none. [y/N] " ans
    if [ "${ans:-N}" = "y" ] || [ "${ans:-N}" = "Y" ]; then
      local h p sec u s al pw
      read -r -p "SMTP host: " h; read -r -p "SMTP port [587]: " p; read -r -p "Security (starttls/ssl/none) [starttls]: " sec
      read -r -p "SMTP login username: " u; read -r -s -p "SMTP password (hidden): " pw; echo
      read -r -p "Sender address (From:, may differ from login): " s
      read -r -p "Allowlist: ONLY these recipients may receive real mail (e.g. you@example.com): " al
      set_env EMAIL_MODE smtp; set_env SMTP_HOST "$h"; set_env SMTP_PORT "${p:-587}"; set_env SMTP_SECURITY "${sec:-starttls}"
      set_env SMTP_USERNAME "$u"; set_env SMTP_PASSWORD "$pw"; set_env SMTP_SENDER "$s"; set_env REAL_SEND_ALLOWLIST "$al"
      echo "  SMTP_PASSWORD     : set (length ${#pw})"
      echo "  Real mode is on. ./run.sh smoke needs EMAIL_MODE=mailpit - set it back temporarily to run the demo."
    fi
  fi
  chmod 600 .env
  echo "next: ./run.sh up"
}

cmd_up() {
  need_docker; need_env
  if [ -z "$(get_env HOST_PORT_UI)" ]; then   # .env created before the web UI existed
    set_env HOST_PORT_UI "$(free_port 8502)"
    info "added HOST_PORT_UI=$(get_env HOST_PORT_UI) to .env (web interface port)"
  fi
  info "checking host ports"
  port_conflicts || die "a host port is taken (see above). Edit HOST_PORT_* in .env, or stop that container, then retry."
  info "building images and starting the stack (first build takes a few minutes)"
  dc up -d --build || die "docker compose up failed. Run: ./run.sh logs"
  local api; api="$(get_env HOST_PORT_API)"
  info "waiting for migrations, API and worker"
  local i
  for i in $(seq 1 90); do
    mx="$(docker inspect -f '{{.State.ExitCode}}' agentic_nl_migrate 2>/dev/null || echo '')"
    if [ -n "$mx" ] && [ "$mx" != "0" ] && [ "$(docker inspect -f '{{.State.Running}}' agentic_nl_migrate 2>/dev/null)" = "false" ]; then
      echo "migration container failed (exit $mx):"; dc logs --tail=40 migrate; exit 1
    fi
    api_ok=0; curl -fsS "http://127.0.0.1:${api}/health" >/dev/null 2>&1 && api_ok=1
    wh="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' agentic_nl_worker 2>/dev/null)"
    if [ "$api_ok" = 1 ] && { [ "$wh" = "healthy" ] || [ "$i" -gt 60 ]; }; then break; fi
    sleep 2
  done
  [ "${api_ok:-0}" = 1 ] || { echo "API did not become healthy in time. Recent logs:"; dc logs --tail=40 api migrate; exit 1; }
  local uip; uip="$(get_env HOST_PORT_UI)"
  for i in $(seq 1 30); do curl -fsS "http://127.0.0.1:${uip}/_stcore/health" >/dev/null 2>&1 && break; sleep 2; done
  curl -fsS "http://127.0.0.1:${uip}/_stcore/health" >/dev/null 2>&1 || echo "WARNING: the web interface is not answering yet. Check: ./run.sh logs ui"
  dc ps
  echo
  echo "Web UI   : http://localhost:${uip}      <- open this in your browser"
  echo "API docs : http://localhost:${api}/docs"
  echo "Mailpit  : http://localhost:$(get_env HOST_PORT_MAILPIT_UI)   (all demo emails land here)"
  echo "next     : ./run.sh smoke"
}

cmd_down()   { need_docker; need_env; dc down; }
cmd_status() { need_docker; need_env; dc ps -a; }
cmd_logs()   { need_docker; need_env; dc logs --tail=200 "$@"; }
cmd_psql()   { need_docker; need_env; docker exec -it agentic_nl_db psql -U "$(get_env POSTGRES_USER)" -d "$(get_env POSTGRES_DB)"; }
cmd_reset() {
  need_docker; need_env
  echo "This deletes ALL data of THIS project only (volume agentic_nl_pgdata, containers agentic_nl_*)."
  read -r -p "Type 'yes' to continue: " a
  [ "$a" = "yes" ] || die "aborted, nothing deleted"
  dc down -v --remove-orphans
}
tools() { need_docker; need_env; dc run --rm --build tools "$@"; }
cmd_test()  { info "running pytest in a container against a throw-away database"; tools python -m pytest -q tests; }
cmd_test_ui() { need_docker; need_env; info "running the web interface tests in a container (no database needed)"; dc run --rm --build ui-tests; }
cmd_smoke() { cmd_up || exit 1; info "running end-to-end smoke test (takes about 2 minutes)"; tools python -m app.smoke; }
cmd_check_llm()   { tools python -m app.cli check-llm; }
cmd_check_smtp()  { tools python -m app.cli check-smtp; }
cmd_check_feeds() { tools python -m app.cli check-feeds; }
cmd_send_test() {
  [ $# -eq 1 ] || die "usage: ./run.sh send-test your-own-address@example.com"
  tools python -m app.cli send-test "$1"
}
cmd_help() {
  cat <<'H'
Usage: ./run.sh <command>      (always from the project root)
  doctor              inspect this machine, ports, Docker; paste the output when asking for help
  init-env            create .env (random secrets, timezone, free ports; optional hidden SMTP prompts)
  up | down | status  start / stop / list the stack
  logs [service]      show recent logs (add -f to follow)
  psql                open psql in the project database
  reset               delete this project's containers and data (asks first)
  test                run the pytest suite in a container (ephemeral database)
  test-ui             run the web interface tests in a container
  smoke               end-to-end check; prints RESULT: PASS or FAIL
  check-llm | check-smtp | check-feeds | send-test <your-address>
H
}

case "${1:-help}" in
  doctor)      cmd_doctor ;;
  init-env)    shift; cmd_init_env "$@" ;;
  up)          cmd_up ;;
  down)        cmd_down ;;
  status)      cmd_status ;;
  logs)        shift; cmd_logs "$@" ;;
  psql)        cmd_psql ;;
  reset)       cmd_reset ;;
  test)        cmd_test ;;
  test-ui)     cmd_test_ui ;;
  smoke)       cmd_smoke ;;
  check-llm)   cmd_check_llm ;;
  check-smtp)  cmd_check_smtp ;;
  check-feeds) cmd_check_feeds ;;
  send-test)   shift; cmd_send_test "$@" ;;
  help|-h|--help) cmd_help ;;
  *) echo "unknown command: $1" >&2; cmd_help; exit 2 ;;
esac
