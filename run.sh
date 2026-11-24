#!/usr/bin/env bash
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
set -uo pipefail

REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$REPO" || exit 1

JOBS=${JOBS:-$( (nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4) )}
PORT=${PORT:-}
ARTIFACTS=${ARTIFACTS:-}
ASSUME_YES=0
QUIET=0
ACTION=""
LOGDIR=${LOGDIR:-$(dirname "$REPO")}
BOARD_ID_HINT="usb-1a86"
# The Arduino Nano ESP32 has no USB-serial bridge chip: it exposes the
# ESP32-S3's own USB-Serial/JTAG unit, Espressif 303a:1001. Linux names that
# under /dev/serial/by-id/ with the product string, macOS calls it usbmodem.
BOARD_ID_HINT_JTAG="USB_JTAG_serial_debug_unit"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn()  { printf '  ! %s\n' "$*" >&2; }
info()  { printf '  %s\n' "$*"; }
die()   { red "error: $*"; exit 1; }

read_reply() {
	if { : < /dev/tty; } 2>/dev/null; then
		read -r "$1" < /dev/tty
	else
		read -r "$1"
	fi
}

ask() {
	[ "$ASSUME_YES" = 1 ] && return 0
	local reply=""
	printf '%s [y/N] ' "$1"
	read_reply reply || return 1
	case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

usage() {
	cat <<'EOF'
Usage: ./run.sh [options] [action]

With no action it opens the interactive menu.

Actions:
  --check        Check the environment and fix what can be fixed
  --build        Build everything from clean sources
  --verify       Check the checksums of a build
  --flash        Write the image to the board (ERASES /etc and /home)
  --test         Run the board test suite, the extra tests, the network
                 tests (with WIFI_SSID and WIFI_PASS) and the factory soak
  --all          check, build, verify, flash and test, in that order
  --repro        Two builds of the same commit and a comparison
  --recover      Put the board's /etc and /home back to factory
  --status       Show existing builds and the state of the board

Options:
  -y, --yes            Do not prompt; assume yes
  -q, --quiet          Send build output to the log only, not to the screen
  -j, --jobs N         Parallel build jobs (default: nproc)
  -p, --port PATH      Serial adapter (default: autodetect)
  -a, --artifacts DIR  Artifacts directory to use
  -h, --help           This help

Environment:
  WIFI_SSID, WIFI_PASS  join this network for the SSH and port tests
  SOAK_ROUNDS           factory boots in the soak (default 20, 0 skips it)
EOF
}

have() { command -v "$1" >/dev/null 2>&1; }

# GNU coreutils spells it sha256sum, macOS (and anything with perl's shasum)
# spells it shasum -a 256. Both accept -c against a SHA256SUMS file.
sha256_cmd() {
	if have sha256sum; then printf 'sha256sum'
	elif have shasum; then printf 'shasum -a 256'
	else return 1
	fi
}

python_with() {
	local candidate
	for candidate in \
		"${PYSERIAL_PYTHON:-}" \
		python3 \
		"$HOME/.local/share/pipx/venvs/esptool/bin/python3" \
		/usr/bin/python3
	do
		[ -n "$candidate" ] || continue
		have "$candidate" || [ -x "$candidate" ] || continue
		if "$candidate" -c "import $1" >/dev/null 2>&1; then
			printf '%s\n' "$candidate"
			return 0
		fi
	done
	return 1
}

python_with_pyserial() {
	local candidate
	for candidate in \
		"${PYSERIAL_PYTHON:-}" \
		python3 \
		"$HOME/.local/share/pipx/venvs/esptool/bin/python3" \
		/usr/bin/python3
	do
		[ -n "$candidate" ] || continue
		have "$candidate" || [ -x "$candidate" ] || continue
		if "$candidate" -c 'import serial' >/dev/null 2>&1; then
			printf '%s\n' "$candidate"
			return 0
		fi
	done
	return 1
}

# GNU df takes -B; BSD df does not, so ask for 1K blocks and do the arithmetic.
disk_free_gb() { df -Pk "$REPO" 2>/dev/null | awk 'NR==2 {printf "%d", $4 / 1048576}'; }

detect_port() {
	local p
	if [ -n "$PORT" ]; then printf '%s\n' "$PORT"; return 0; fi
	for p in /dev/serial/by-id/*"$BOARD_ID_HINT"*; do
		[ -e "$p" ] && { printf '%s\n' "$p"; return 0; }
	done
	for p in /dev/serial/by-id/*"$BOARD_ID_HINT_JTAG"*; do
		[ -e "$p" ] && { printf '%s\n' "$p"; return 0; }
	done
	for p in /dev/serial/by-id/*; do
		[ -e "$p" ] && { printf '%s\n' "$p"; return 0; }
	done
	# macOS. cu.* rather than tty.*: the tty.* nodes block on open waiting for
	# carrier detect, which never comes on a USB CDC device. The board's own
	# USB-Serial/JTAG unit is a usbmodem; usbserial-/wchusbserial- are external
	# adapters people wire to the UART pins, so they are the fallback.
	for p in /dev/cu.usbmodem* /dev/cu.usbserial-* /dev/cu.wchusbserial*; do
		[ -e "$p" ] && { printf '%s\n' "$p"; return 0; }
	done
	for p in /dev/ttyACM0 /dev/ttyACM1 /dev/ttyUSB0 /dev/ttyUSB1; do
		[ -e "$p" ] && { printf '%s\n' "$p"; return 0; }
	done
	return 1
}

port_holders() {
	local target
	target=$(readlink -f "$1" 2>/dev/null || printf '%s' "$1")
	# macOS has lsof but no fuser; most Linux systems have fuser in psmisc but
	# not necessarily lsof. Either one gives the pids holding the device.
	if have fuser; then
		fuser "$target" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true
	elif have lsof; then
		lsof -t "$target" 2>/dev/null || true
	fi
}

free_port() {
	local port pids pid
	port=$1
	pids=$(port_holders "$port")
	[ -z "$pids" ] && return 0
	warn "the port is held by:"
	for pid in $pids; do
		info "  pid $pid  $(ps -o args= -p "$pid" 2>/dev/null | cut -c1-70)"
	done
	warn "an open console steals bytes and also resets the board when it opens"
	if ask "  Close those processes?"; then
		for pid in $pids; do kill "$pid" 2>/dev/null; done
		sleep 1
		for pid in $(port_holders "$port"); do kill -9 "$pid" 2>/dev/null; done
		sleep 1
		[ -z "$(port_holders "$port")" ] && { green "  port released"; return 0; }
		warn "could not release it"
		return 1
	fi
	return 1
}

builds() { ls -dt "$REPO"/build-output/reproduce.*/artifacts 2>/dev/null; }

latest_artifacts() {
	if [ -n "$ARTIFACTS" ]; then printf '%s\n' "$ARTIFACTS"; return 0; fi
	local first
	first=$(builds | head -1)
	[ -n "$first" ] && { printf '%s\n' "$first"; return 0; }
	return 1
}

clean_tree_or_fix() {
	local dirty stray f
	dirty=$(git -C "$REPO" status --porcelain 2>/dev/null)
	[ -z "$dirty" ] && return 0
	warn "the tree is not clean and reproduce.sh snapshots HEAD:"
	printf '%s\n' "$dirty" | sed 's/^/      /'
	stray=$(printf '%s\n' "$dirty" | awk '$1=="??" && $2 ~ /\.log$/ {print $2}')
	if [ -n "$stray" ]; then
		if ask "  Move the stray .log files out of the repository?"; then
			for f in $stray; do
				mv "$REPO/$f" "$LOGDIR/" 2>/dev/null && info "moved: $f -> $LOGDIR/"
			done
		fi
	fi
	dirty=$(git -C "$REPO" status --porcelain 2>/dev/null)
	[ -z "$dirty" ] && { green "  tree is clean"; return 0; }
	warn "there are uncommitted changes; commit or stash them before building"
	return 1
}

check_env() {
	bold "== Environment =="
	local problems=0 py free port

	for c in git docker python3 awk; do
		if have "$c"; then info "ok       $c"; else red "  missing  $c"; problems=$((problems+1)); fi
	done
	if sha_cmd=$(sha256_cmd); then info "ok       $sha_cmd"; else red "  missing  sha256sum or shasum"; problems=$((problems+1)); fi

	if docker info >/dev/null 2>&1; then
		info "ok       docker responds"
	else
		red "  problem  docker does not respond (service stopped or missing group permission)"
		info "         try: systemctl --user start docker  |  sudo usermod -aG docker \$USER"
		problems=$((problems+1))
	fi

	if have esptool || have esptool.py; then
		info "ok       esptool"
	else
		red "  missing  esptool"
		info "         install with: pipx install esptool   (or pip install --user esptool)"
		problems=$((problems+1))
	fi

	if py=$(python_with_pyserial); then
		info "ok       pyserial on $py"
	else
		red "  missing  pyserial (the board test suite needs it)"
		info "         install with: pipx inject esptool pyserial   (or pip install --user pyserial)"
		problems=$((problems+1))
	fi

	free=$(disk_free_gb)
	if [ -n "$free" ] && [ "$free" -ge 25 ] 2>/dev/null; then
		info "ok       disk: ${free} GB free"
	else
		red "  problem  disk: ${free:-?} GB free; one build takes about 21 GB"
		problems=$((problems+1))
	fi

	if port=$(detect_port); then
		info "ok       board on $port"
		[ -n "$(port_holders "$port")" ] && warn "the port is busy (see the release option)"
	else
		warn "no serial adapter detected; connect the board to flash it"
	fi

	if [ -d "$REPO/.git" ]; then
		info "ok       commit $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null) on $(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)"
	fi

	echo
	if [ "$problems" -eq 0 ]; then green "No problems."; else red "$problems problem(s) to resolve."; fi
	return "$problems"
}

do_status() {
	bold "== Builds =="
	local a manifest
	if [ -z "$(builds)" ]; then
		info "none yet"
	else
		while IFS= read -r a; do
			manifest="$a/build-manifest.json"
			printf '  %s\n' "${a#$REPO/}"
			if [ -f "$manifest" ]; then
				python3 - "$manifest" <<'PY' 2>/dev/null || true
import json, sys
m = json.load(open(sys.argv[1]))
v = m.get('board_verification')
print('      commit', m.get('source_commit', '?')[:12],
      '| image', m.get('sha256', {}).get('linux-esp32s3-native-full.bin', '?')[:16],
      '| board', v if isinstance(v, str) else v.get('status', '?'))
PY
			fi
		done < <(builds)
	fi
	echo
	bold "== Board =="
	local port
	if port=$(detect_port); then
		info "port $port"
		local h; h=$(port_holders "$port")
		[ -n "$h" ] && warn "held by: $h" || info "free"
	else
		info "not detected"
	fi
}

do_build() {
	bold "== Build from clean sources =="
	have docker || die "docker is missing"
	docker info >/dev/null 2>&1 || die "docker does not respond"
	local free; free=$(disk_free_gb)
	if [ -n "$free" ] && [ "$free" -lt 25 ] 2>/dev/null; then
		warn "only ${free} GB free and the build takes about 21 GB"
		ask "  Continue anyway?" || return 1
	fi
	# reproduce.sh is Linux only: GNU df, a clean-tree requirement, and a
	# git-archive snapshot of HEAD that would silently drop uncommitted work.
	# The macOS wrapper handles all three, and additionally resumes from the
	# stage markers instead of recompiling the toolchain every run.
	local script="$REPO/build/reproduce.sh" emulated=0
	if [ "$(uname -s)" = Darwin ]; then
		script="$REPO/build/reproduce-macos.sh"
		emulated=1
		[ -x "$script" ] || die "$script is missing or not executable"
	else
		clean_tree_or_fix || return 1
	fi
	local log="$LOGDIR/esp32-build-$(date +%Y%m%d-%H%M%S).log"
	info "jobs:  $JOBS"
	info "log:   $log"
	echo
	bold "This downloads and compiles a cross toolchain, the kernel, the"
	bold "firmware and the userspace from source. It takes a long time:"
	if [ "$emulated" = 1 ]; then
		bold "under amd64 emulation on Apple Silicon, hours on the first run."
		bold "Later runs resume from the stage markers and take minutes."
	else
		bold "roughly 40 minutes on 8 jobs, longer on fewer."
	fi
	bold "It needs about 21 GB. The build output scrolls below as it happens."
	echo
	[ "$QUIET" = 1 ] && info "(quiet: output only goes to the log)"
	ask "  Start the build?" || { info "cancelled"; return 1; }
	echo
	local started rc elapsed
	started=$(date +%s)
	if [ "$QUIET" = 1 ]; then
		JOBS="$JOBS" bash "$script" > "$log" 2>&1
		rc=$?
	else
		JOBS="$JOBS" bash "$script" 2>&1 | tee "$log"
		rc=${PIPESTATUS[0]}
	fi
	elapsed=$(( $(date +%s) - started ))
	echo
	info "elapsed: $((elapsed / 60))m $((elapsed % 60))s"
	if [ "$rc" -ne 0 ]; then
		red "the build failed (code $rc)"
		info "last lines of $log:"
		tail -15 "$log" | sed 's/^/      /'
		return 1
	fi
	ARTIFACTS=$(builds | head -1)
	green "done: ${ARTIFACTS#$REPO/}"
}

do_verify() {
	bold "== Check checksums =="
	local a; a=$(latest_artifacts) || { red "no artifacts; build first"; return 1; }
	info "${a#$REPO/}"
	[ -f "$a/SHA256SUMS" ] || { red "SHA256SUMS is missing"; return 1; }
	if ( cd "$a" && $(sha256_cmd) -c SHA256SUMS ) | sed 's/^/  /'; then
		green "every artifact matches"
		return 0
	fi
	red "some artifacts do not match their checksum"
	return 1
}

do_flash() {
	bold "== Flash =="
	local a port
	a=$(latest_artifacts) || { red "no artifacts; build first"; return 1; }
	port=$(detect_port) || { red "no board detected; pass -p PATH"; return 1; }
	info "image: ${a#$REPO/}"
	info "port:  $port"
	red   "this ERASES /etc and /home on the board"
	ask "  Write the image?" || { info "cancelled"; return 1; }
	free_port "$port" || { red "release the port and try again"; return 1; }
	"$REPO/flash.sh" -p "$port" --images "$a"
	local rc=$?
	[ "$rc" -eq 0 ] && green "flashing finished" || red "flashing failed (code $rc)"
	return "$rc"
}

report() {
	python3 - "$1" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
bad = [t['name'] for t in r['tests'] if t['status'] != 'pass']
print(f"  {r['status'].upper()}: {len(r['tests'])} tests, {len(bad)} failed")
for name in bad:
    print('    failed:', name)
PY
}

do_test() {
	bold "== Board test suite =="
	local a port py net out n rc=0 summary="" ip tool rounds
	a=$(latest_artifacts) || { red "no artifacts"; return 1; }
	port=$(detect_port) || { red "no board detected"; return 1; }
	py=$(python_with_pyserial) || { red "no python with pyserial"; info "install with: pipx inject esptool pyserial"; return 1; }
	free_port "$port" || return 1
	out="$REPO/build-output/board-check"
	n=2
	while [ -e "$out" ]; do out="$REPO/build-output/board-check-$n"; n=$((n+1)); done
	info "output: ${out#$REPO/}"

	info "suite: 36 tests, about 5 minutes"
	"$py" "$REPO/build/test-board.py" "$port" "$a" --output "$out/suite" --reset-from-bootloader
	if [ $? -eq 0 ]; then summary="$summary suite:pass"; else summary="$summary suite:FAIL"; rc=1; fi
	[ -f "$out/suite/results.json" ] && report "$out/suite/results.json"

	bold "== Extra board tests =="
	"$py" "$REPO/build/extra-board-tests.py" "$port" "$out/extra"
	if [ $? -eq 0 ]; then summary="$summary extra:pass"; else summary="$summary extra:FAIL"; rc=1; fi

	bold "== Network: SSH, ports and auth =="
	if [ -z "${WIFI_SSID:-}" ] || [ -z "${WIFI_PASS:-}" ]; then
		warn "WIFI_SSID and WIFI_PASS are not set; the network tests are skipped"
		summary="$summary network:skipped"
	elif ! net=$(python_with paramiko); then
		warn "no python with paramiko; the network tests are skipped"
		info "install with: pip install --user paramiko"
		summary="$summary network:skipped"
	elif ! ip=$("$py" "$REPO/build/board-wifi.py" "$port"); then
		red "the board did not join $WIFI_SSID"
		summary="$summary network:FAIL"; rc=1
	else
		info "board at $ip"
		if "$net" "$REPO/build/test-ssh-pty.py" "$port" --output "$out/ssh-pty" &&
			"$net" "$REPO/build/test-network-services.py" "$port" --output "$out/network-services"; then
			summary="$summary network:pass"
		else
			summary="$summary network:FAIL"; rc=1
		fi
	fi

	rounds=${SOAK_ROUNDS:-20}
	if [ "$rounds" -gt 0 ] 2>/dev/null; then
		bold "== Factory boot soak: $rounds rounds =="
		info "each round rewrites /etc and /home, about a minute and a half each"
		tool=$(have esptool && echo esptool || echo esptool.py)
		"$py" "$REPO/build/soak-boot.py" "$port" "$a" --output "$out/soak" --rounds "$rounds" --esptool "$tool"
		if [ $? -eq 0 ]; then summary="$summary soak:pass"; else summary="$summary soak:FAIL"; rc=1; fi
	else
		summary="$summary soak:skipped"
	fi

	info "putting the login back to the factory one"
	"$py" "$REPO/build/factory-login.py" "$port" ||
		warn "could not reset it; run: $py build/factory-login.py $port"
	bold "== Summary =="
	for item in $summary; do info "${item%%:*}: ${item#*:}"; done
	if [ "$rc" -ne 0 ]; then
		red "not everything passed"
		warn "if the suite was cut short, test users are left on the board;"
		warn "use the recover option before retrying"
		return 1
	fi
	green "every test passed"
}

do_recover() {
	bold "== Restore a board to factory =="
	# There is nothing to restore any more. --recover used to rewrite the etc
	# and home jffs2 partitions from the last build's artifacts, putting a
	# used board back to a fresh-flash state without touching the kernel or
	# the rootfs. Both partitions are gone: /etc and /home are inside the
	# read-only cramfs, so there is no writable state on the board to put
	# back, and the artifacts no longer contain etc.jffs2 or home.jffs2.
	#
	# A full reflash is what does it now, and that is what the message says
	# rather than failing on a missing file.
	yellow "there are no separate /etc or /home partitions to restore"
	info "  /etc and /home are part of the read-only rootfs; the board keeps no"
	info "  writable state, so a fresh flash is already a factory state."
	info "  use --flash (or ./flash.sh) to write the whole image."
	return 0
}

do_repro() {
	bold "== Reproducibility: two builds of the same commit =="
	local first second
	first=$(builds | head -1)
	if [ -z "$first" ]; then
		info "no build yet; making the first one"
		do_build || return 1
		first=$(builds | head -1)
	else
		info "first: ${first#$REPO/}"
	fi
	info "now the second one, same commit"
	do_build || return 1
	second=$(builds | head -1)
	if [ "$second" = "$first" ]; then red "a second build did not appear"; return 1; fi
	bold "== Comparison =="
	python3 "$REPO/build/compare-builds.py" "${first%/artifacts}" "${second%/artifacts}"
}

do_all() {
	check_env || { ask "  There are problems. Continue anyway?" || return 1; }
	do_build   || return 1
	do_verify  || return 1
	do_flash   || return 1
	do_test    || return 1
	green "complete path finished"
}

menu() {
	while true; do
		echo
		bold "=== Linux on ESP32-S3 ==="
		cat <<'EOF'
  1) Check the environment
  2) Build everything from clean sources
  3) Check the checksums of a build
  4) Flash the board
  5) Run the board tests
  6) EVERYTHING: build, check, flash and test
  7) Reproducibility: two builds and a comparison
  8) Recover the board (restore /etc and /home)
  9) Status
  0) Quit
EOF
		printf 'Choice: '
		local choice=""
		read_reply choice || { echo; return 0; }
		case "$choice" in
			1) check_env ;;
			2) do_build ;;
			3) do_verify ;;
			4) do_flash ;;
			5) do_test ;;
			6) do_all ;;
			7) do_repro ;;
			8) do_recover ;;
			9) do_status ;;
			0|q|Q) return 0 ;;
			*) warn "invalid choice" ;;
		esac
	done
}

while [ $# -gt 0 ]; do
	case "$1" in
		-y|--yes)        ASSUME_YES=1; shift ;;
		-q|--quiet)      QUIET=1; shift ;;
		-j|--jobs)       JOBS="${2:?-j needs a number}"; shift 2 ;;
		-p|--port)       PORT="${2:?-p needs a path}"; shift 2 ;;
		-a|--artifacts)  ARTIFACTS="${2:?-a needs a directory}"; shift 2 ;;
		-h|--help)       usage; exit 0 ;;
		--check|--build|--verify|--flash|--test|--all|--repro|--recover|--status)
		                 ACTION="${1#--}"; shift ;;
		*)               red "unknown option: $1"; usage; exit 1 ;;
	esac
done

if [ -n "$ARTIFACTS" ] && [ ! -d "$ARTIFACTS" ]; then
	die "artifacts directory does not exist: $ARTIFACTS"
fi

case "$ACTION" in
	check)   check_env ;;
	build)   do_build ;;
	verify)  do_verify ;;
	flash)   do_flash ;;
	test)    do_test ;;
	all)     do_all ;;
	repro)   do_repro ;;
	recover) do_recover ;;
	status)  do_status ;;
	"")      menu ;;
esac
