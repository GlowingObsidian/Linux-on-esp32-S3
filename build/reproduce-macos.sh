#!/usr/bin/env bash
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
#
# reproduce.sh for macOS (Apple Silicon and Intel).
#
# reproduce.sh itself cannot run here: it uses GNU `df -PBG`, it refuses to
# start unless the tree is clean, and it snapshots HEAD with `git archive`,
# so uncommitted work would be silently left out of the image. It also makes a
# fresh mktemp directory per run, which throws away the stage markers in
# build/container-build.sh and recompiles the crosstool-NG toolchain (hours)
# every single time.
#
# This wrapper fixes all four and changes nothing about what gets built:
#   * df -Pk, which BSD df understands
#   * the working tree is snapshotted, not HEAD, so uncommitted edits build
#   * a persistent work directory, so completed stages resume
#   * --platform linux/amd64, because the pinned base image digest in
#     build/Dockerfile is the amd64 manifest and will not pull on arm64
#
# The build output is streamed to the terminal in full. Nothing is quiet.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"

IMAGE=${IMAGE:-linux-esp32s3-reproduce:local}
LOG_CAP_BYTES=${LOG_CAP_BYTES:-50000000}
# Docker Desktop runs amd64 images under emulation on Apple Silicon.
PLATFORM=${PLATFORM:-linux/amd64}
# Under Rosetta each compiler process costs noticeably more memory than it
# would natively, and Docker Desktop's VM is usually far smaller than the
# host's RAM (7.75 GB on a 16 GB Mac, by default). -j8 in that much memory
# gets the kernel OOM killer involved partway through the toolchain build,
# which costs hours. 6 keeps most of the parallelism and leaves headroom.
JOBS=${JOBS:-6}
# Downstream stages that must be re-run when this project's sources change.
# `toolchain` is deliberately not in this list: it depends only on the pinned
# revisions in build/sources.lock, so it survives day-to-day edits. `espctl` is
# in it and is not optional: the stage compiles a source file from this tree, so
# leaving it out would let an edit to espctl.c keep a stale binary and the build
# would report success with the old program in the image. `blob-libs` is in it
# for the same reason the toolchain is not: it fetches, it does not build, and
# what it fetches is pinned by sources.lock -- so its inputs are that file and
# the script that applies this project's changes to the clone.
DOWNSTREAM="espctl blob-libs base-rootfs firmware userspace package"

# What each downstream stage actually reads, as a set of paths relative to the
# snapshot root. A stage is re-run when its own inputs move, not when anything
# anywhere in the tree moves.
#
# This replaces a single whole-tree hash that invalidated every stage on any
# edit. That gate was correct but far too coarse: adding one file under
# patches/linux re-ran the firmware and both rootfs stages, which is hours for
# sources they never read. The container already fingerprints the fine-grained
# cases it knows about -- rootfs_base stamps patches/linux, userspace stamps the
# kernel config and patches together -- but those stamps live inside stages the
# coarse gate had already thrown away, so they never got the chance to say "this
# one is unchanged".
#
# The lists are deliberately generous where a stage's true input set is hard to
# name, and each one is a superset of what the stage reads. Over-inclusion costs
# a stage re-run; under-inclusion ships a stale artifact, which is the failure
# this whole mechanism exists to prevent. `package` therefore depends on all
# four stages before it, because it copies their outputs.
stage_inputs() {
	case "$1" in
	espctl)
		printf '%s\n' \
			'new-files/board/espressif/esp32s3/espctl' \
			'experiments/mmu-poc/programs/env.sh'
		;;
	blob-libs)
		# This stage only fetches: it clones esp-hosted and checks out the
		# ESP-IDF submodule, so the six Wi-Fi archives exist before the
		# kernel links in base-rootfs. Its inputs are therefore the pinned
		# revisions in sources.lock and the script that applies this
		# project's esp-hosted changes, which is what decides which tree
		# the submodule is checked out under.
		printf '%s\n' \
			'build/sources.lock' \
			'new-files/toplevel/apply-local-changes.sh'
		;;
	base-rootfs)
		# Buildroot's config, the overlay it copies in, its package patches,
		# the kernel patches (this stage builds the rootfs' own kernel), and
		# the defconfig that names the board.
		#
		# nano_esp32_linux.config is here because this stage also builds
		# buildroot's own xipImage, and that config is what selects the
		# drivers in it. `package` copies that xipImage, not the fork's, so
		# a config change that only the `userspace` list saw would leave
		# buildroot's kernel built from the previous config and package it:
		# the fork tree would be right, the shipped image wrong, and the
		# only symptom a size assertion at the very end.
		printf '%s\n' \
			'new-files/board/espressif/esp32s3/rootfs_overlay' \
			'new-files/board/espressif/esp32s3/package-patches' \
			'new-files/board/espressif/esp32s3/patches' \
			'new-files/board/espressif/esp32s3/busybox.config' \
			'new-files/board/espressif/esp32s3/trim-target.sh' \
			'new-files/board/espressif/esp32s3/nano_esp32_linux.config' \
			'new-files/configs' \
			'new-files/toplevel'
		;;
	firmware)
		# The ESP-IDF side: the network adapter, its sdkconfig, and the
		# patch that is applied to esp-hosted.
		printf '%s\n' \
			'new-files/esp-hosted' \
			'build/test-nano-memory-config.py' \
			'patches/02-firmware-network-adapter.patch' \
			'make-images.sh'
		;;
	userspace)
		# Everything the fork, the programs and the cramfs profiles read.
		# nano_esp32_linux.config is here because this stage builds the
		# xipImage the board runs, and that config selects the drivers in
		# it: the stage's own fork_stamp fingerprints the config and the
		# patches together for exactly this reason, and leaving the config
		# out of this list would discard the stamp before it could act.
		printf '%s\n' \
			'experiments/mmu-poc' \
			'new-files/board/espressif/esp32s3/patches' \
			'new-files/board/espressif/esp32s3/nano_esp32_linux.config' \
			'new-files/board/espressif/esp32s3/rootfs_overlay' \
			'build/package-final.py' \
			'build/patch-psram-libatomic.py' \
			'build/install-default-tools.py'
		;;
	package)
		# `package` reads the other four stages' outputs, so it must re-run
		# whenever any of them does -- it copies xipImage out of
		# experiments/mmu-poc/out/real-bins, which `userspace` builds, and
		# bootloader.bin, partition-table.bin and network_adapter.bin out of
		# base-images, which `firmware` builds. Listing only its own inputs
		# here would leave it packaging the previous kernel after a kernel
		# patch, which is the exact stale-artifact failure this gate exists
		# to prevent. So its set is the union of the four before it.
		stage_inputs userspace
		stage_inputs firmware
		stage_inputs base-rootfs
		stage_inputs blob-libs
		stage_inputs espctl
		printf '%s\n' \
			'build/package-final.py' \
			'new-files/board/espressif/esp32s3/package-patches' \
			'new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r'
		;;
	*)
		die "no input list for stage '$1'"
		;;
	esac
}

# Hash a stage's declared inputs. Missing paths contribute nothing rather than
# failing: a stage whose inputs are all absent has nothing to have changed.
stage_hash() {
	local rel
	{
		case "$1" in
		userspace|package) git show -s --format=%ct HEAD ;;
		esac
		for rel in $(stage_inputs "$1"); do
			if [ -e "$work/Linux-on-esp32-S3/$rel" ]; then
				printf '%s\n' "$rel"
				( cd "$work/Linux-on-esp32-S3" && \
					find "$rel" -type f -not -path '*/__pycache__/*' \
						-not -name '.DS_Store' -print0 | sort -z | \
					xargs -0 shasum -a 256 )
			fi
		done
	} | shasum -a 256 | awk '{print $1}'
}

work="$repo/build-output/reproduce.macos"
fresh=0

red()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
warn() { printf '  ! %s\n' "$*" >&2; }
info() { printf '  %s\n' "$*"; }
die()  { red "error: $*"; exit 1; }

usage() {
	cat <<'EOF'
Usage: build/reproduce-macos.sh [options]

Builds the image on macOS. Equivalent to build/reproduce.sh, which is Linux
only. The whole build log goes to the terminal; there is no quiet mode.

Options:
  --fresh          Throw away the work directory and build from scratch
  --work DIR       Use DIR instead of build-output/reproduce.macos
  -j, --jobs N     Parallel build jobs (default: 6, see below)
  -h, --help       This help

Environment:
  JOBS             Same as --jobs. The default is lower than reproduce.sh's 8
                   because the Docker VM is much smaller than the host's RAM
                   and Rosetta inflates each compiler's footprint.
  IMAGE            Docker image tag (default: linux-esp32s3-reproduce:local)
  PLATFORM         Build platform (default: linux/amd64)
  REFS_VOL         Docker volume holding the compiled tree, which must be a
                   case-sensitive filesystem. Derived from the work directory
                   by default; override to share or isolate one by hand.
  OUT_VOL          Docker volume for experiments/mmu-poc/out. The `userspace`
                   stage copies the Linux kernel tree into it, so it must be
                   case-sensitive too. Derived from the work directory.
  BUILD_NET        Docker network for the build; unset means the default
                   bridge, which is what Docker Desktop supports
  LOG_CAP_BYTES    Cap on the on-disk copy of each log (default: 50 MB)
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
		--fresh)      fresh=1; shift ;;
		--work)       work="${2:?--work needs a directory}"; shift 2 ;;
		-j|--jobs)    JOBS="${2:?--jobs needs a number}"; shift 2 ;;
		-h|--help)    usage; exit 0 ;;
		*)            red "unknown option: $1"; usage; exit 1 ;;
	esac
done

[ "$(id -u)" != 0 ] || die "run as a regular user with Docker access"
command -v docker >/dev/null 2>&1 || die "docker is missing"
docker info >/dev/null 2>&1 || die "docker does not respond (start Docker Desktop)"
command -v rsync >/dev/null 2>&1 || die "rsync is missing"
command -v shasum >/dev/null 2>&1 || die "shasum is missing"

# The tree crosstool-NG, Buildroot, the kernel and esp-idf actually compile in
# is $work/refs, and it is mounted from a Docker volume rather than from the
# host. macOS's APFS is case-insensitive by default, and crosstool-NG refuses
# to run on a case-insensitive filesystem -- "Your file system in '.../.build'
# is *not* case-sensitive!" -- because several of its source trees contain
# files whose names differ only in case. A Docker volume is ext4 inside the VM,
# so it is case-sensitive. Everything else (sources, logs, stage markers,
# artifacts) stays on the host bind mount, so logs still stream live and the
# artifacts still land in the repository as usual.
# Two directories need that, for two different reasons.
#
# refs/ is the compiled tree, and crosstool-NG refuses to configure at all on a
# case-insensitive filesystem.
#
# out/ is where the `userspace` stage copies source trees into, and one of them
# is the Linux kernel. Its netfilter directories contain names that differ only
# in case -- include/uapi/linux/netfilter alone holds both xt_DSCP.h and
# xt_dscp.h, and net/netfilter both xt_rateest.c and xt_RATEEST.c. On APFS the
# second name of each pair cannot be created, so the copy dies with "File
# exists" 13 files short of a 97,898-file tree and takes the stage with it.
# Buildroot's copy of the same kernel is unaffected because it lives in refs/.
#
# This is not something a copy flag can fix: no cp invocation, and no rsync,
# can put two names that fold together onto a filesystem that folds them.
WORK_HASH=$(printf '%s' "$work" | shasum -a 256 | awk '{print substr($1,1,12)}')
REFS_VOL=${REFS_VOL:-$(printf 'esp32-refs-%s' "$WORK_HASH")}
OUT_VOL=${OUT_VOL:-$(printf 'esp32-out-%s' "$WORK_HASH")}
OUT_PATH=Linux-on-esp32-S3/experiments/mmu-poc/out

# One build takes about 21 GB. df -Pk is the portable spelling; -B is GNU only.
free_gb=$(df -Pk "$repo" 2>/dev/null | awk 'NR==2 {printf "%d", $4 / 1048576}')
if [ -z "$free_gb" ] || [ "$free_gb" -lt 25 ]; then
	die "${free_gb:-an unknown amount of} GB free; one build takes about 21 GB.
       Free some space (build-output/ is the usual place) before building."
fi

# The toolchain under emulation is the long pole. Rosetta makes it many times
# faster than QEMU, and it is a checkbox in Docker Desktop that is easy to miss.
printf '\n== environment ==\n'
info "repo:      $repo"
info "platform:  $PLATFORM (emulated on Apple Silicon)"
info "jobs:      $JOBS"
info "disk:      ${free_gb} GB free"
info "work:      ${work#$repo/}"
info "build dir: docker volume $REFS_VOL -> ${work#$repo/}/refs"
info "scratch:   docker volume $OUT_VOL -> ${work#$repo/}/experiments/mmu-poc/out"

if [ "$fresh" = 1 ]; then
	printf '  --fresh: removing %s and volumes %s, %s\n' "${work#$repo/}" "$REFS_VOL" "$OUT_VOL"
	rm -rf "$work"
	docker volume rm -f "$REFS_VOL" "$OUT_VOL" >/dev/null 2>&1 || true
fi
# A refs/ or out/ directory left on the host is shadowed by its volume, and
# confuses whoever goes looking for the build tree. It also matters for
# correctness: hash_of below reads this directory, so a stale copy of a build
# product left here would make every later run look like a source change.
if [ -e "$work/refs" ] && [ ! -L "$work/refs" ]; then rm -rf "$work/refs"; fi
if [ -e "$work/$OUT_PATH" ] && [ ! -L "$work/$OUT_PATH" ]; then rm -rf "$work/$OUT_PATH"; fi
mkdir -p "$work/Linux-on-esp32-S3" "$work/logs" "$work/stages" "$work/artifacts"

printf '\n== snapshot ==\n'
# The working tree, not HEAD: uncommitted edits are the whole point here.
# images/ is the project's released binaries, 29 MB the build does not read;
# build-output/ is this build's own output and would recurse.
#
# This file is excluded because it never runs inside the container: it is the
# host-side entry point that launches it. hash_of below decides which stages
# still describe these sources, so leaving it in would mean every tweak to the
# wrapper discarded the stage markers and rebuilt the firmware for nothing.
rsync -a --delete \
	--exclude '.git' \
	--exclude 'images' \
	--exclude 'build-output' \
	--exclude '__pycache__' \
	--exclude '.DS_Store' \
	--exclude 'experiments/mmu-poc/out' \
	--exclude 'build/reproduce-macos.sh' \
	./ "$work/Linux-on-esp32-S3/"
snapshot_files=$(find "$work/Linux-on-esp32-S3" -type f | wc -l | tr -d ' ')
info "copied $snapshot_files files"

# Content hash of the snapshot: this is what decides whether the stage markers
# from the last run still describe these sources.
#
# It has to see exactly what the rsync above copied, so it carries the same
# excludes. Walking the whole tree instead made the hash change on every run
# and threw the resume markers away every time: `images/` and this script are
# excluded from the copy but still live in the snapshot directory, and the
# build rewrites `images/` as its own output. The next run then saw "sources
# changed" and re-ran every stage -- hours, for sources nothing had
# touched. The `espctl` binary is listed for the same reason: the espctl stage
# compiles it into the snapshot, and it is a build product, not a source.
hash_of() { (cd "$1" && find . -type f \
	-not -path './images/*' \
	-not -path './build-output/*' \
	-not -path '*/__pycache__/*' \
	-not -name '.DS_Store' \
	-not -path './experiments/mmu-poc/out/*' \
	-not -path './build/reproduce-macos.sh' \
	-not -path './new-files/board/espressif/esp32s3/rootfs_overlay/usr/bin/espctl' \
	-print0 | sort -z | xargs -0 shasum -a 256) | shasum -a 256 | awk '{print $1}'; }
new_tree=$(hash_of "$work/Linux-on-esp32-S3")
# The toolchain depends only on these two, so an edit to a shell script must
# not cost another crosstool-NG build.
new_tc=$( (shasum -a 256 "$work/Linux-on-esp32-S3/build/sources.lock" \
	"$work/Linux-on-esp32-S3/build/pin-toolchain.py" 2>/dev/null || true) \
	| shasum -a 256 | awk '{print $1}')
old_tree=$(cat "$work/tree-hash" 2>/dev/null || echo none)
old_tc=$(cat "$work/toolchain-hash" 2>/dev/null || echo none)

if [ "$new_tc" != "$old_tc" ]; then
	echo '  toolchain inputs changed; every stage will re-run'
	rm -f "$work/stages/"*.done
elif [ "$new_tree" != "$old_tree" ]; then
	# Sources moved. Drop only the stage markers whose own inputs moved, so a
	# kernel patch re-runs the kernel and the packaging and leaves the
	# firmware and the rootfs alone. The per-stage hashes are written after
	# the decision, so a stage that is kept keeps the hash it was kept for.
	echo '  sources changed; keeping the toolchain'
	for name in $DOWNSTREAM; do
		new_stage=$(stage_hash "$name")
		old_stage=$(cat "$work/stage-hash-$name" 2>/dev/null || echo none)
		if [ "$new_stage" != "$old_stage" ]; then
			echo "    $name: inputs changed, will re-run"
			rm -f "$work/stages/$name.done"
		else
			echo "    $name: inputs unchanged, keeping"
		fi
		printf '%s\n' "$new_stage" > "$work/stage-hash-$name"
	done
else
	# Nothing moved, or this is a first run with no recorded hashes. Either
	# way, record what each stage's inputs are now: a stage marker that
	# survives into the next run must be judged against the inputs it was
	# actually built from, and on a first run `--fresh` has just removed
	# both the markers and these files.
	for name in $DOWNSTREAM; do
		printf '%s\n' "$(stage_hash "$name")" > "$work/stage-hash-$name"
	done
fi
printf '%s\n' "$new_tc" > "$work/toolchain-hash"
printf '%s\n' "$new_tree" > "$work/tree-hash"

# build/package-final.py:189-190 reads exactly these two files.
commit=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain)" ]; then
	commit="$commit-dirty"
fi
printf '%s\n' "$commit" > "$work/source-commit.txt"
git show -s --format=%ct HEAD > "$work/source-epoch.txt"
info "commit:    $commit"

printf '\n== container image ==\n'
# macOS ships bash 3.2, where expanding an empty array under `set -u` is an
# error, hence the ${arr[@]+...} spelling rather than a bare "${arr[@]}".
build_net=()
[ -n "${BUILD_NET:-}" ] && build_net=(--network "$BUILD_NET")
docker build --platform "$PLATFORM" ${build_net[@]+"${build_net[@]}"} \
	--build-arg BUILDER_UID="$(id -u)" --build-arg BUILDER_GID="$(id -g)" \
	-f build/Dockerfile -t "$IMAGE" . 2>&1 |
	tee >(head -c "$LOG_CAP_BYTES" > "$work/logs/container-image.log")
docker inspect --format '{{.Id}}' "$IMAGE" > "$work/container-image-id.txt"

docker volume create "$REFS_VOL" >/dev/null
docker volume create "$OUT_VOL" >/dev/null
# A fresh Docker volume is root-owned and mode 755, so the build user cannot
# create anything in it. Hand them over once; the ownership persists with the
# volumes, so this is a no-op on later runs.
docker run --rm --platform "$PLATFORM" --user 0 \
	--mount "type=volume,source=$REFS_VOL,target=/work/refs" \
	--mount "type=volume,source=$OUT_VOL,target=/work/out" \
	--entrypoint sh "$IMAGE" -c "chown $(id -u):$(id -g) /work/refs /work/out" >/dev/null

# Prove both volumes are writable and case-sensitive before committing hours to
# them: on a case-insensitive filesystem these two names collapse into one file,
# which is what makes crosstool-NG refuse to build and what stops the kernel
# tree copy thirteen files short of the end.
probe=$(docker run --rm --platform "$PLATFORM" \
	--mount "type=volume,source=$REFS_VOL,target=/work/refs" \
	--mount "type=volume,source=$OUT_VOL,target=/work/out" \
	--entrypoint sh "$IMAGE" -c '
		for d in /work/refs /work/out; do
			if ! : > "$d/.esp-case-probe" 2>/dev/null; then
				echo "$d: not writable by uid $(id -u)"; continue
			fi
			: > "$d/.ESP-CASE-PROBE"
			n=$(ls -a "$d" | grep -ci "^\.esp-case-probe$")
			rm -f "$d/.esp-case-probe" "$d/.ESP-CASE-PROBE"
			if [ "$n" = 2 ]; then echo "$d: ok"; else echo "$d: not case-sensitive"; fi
		done' 2>&1)
if [ "$(printf '%s\n' "$probe" | grep -c ': ok$')" != 2 ]; then
	die "a build volume is unusable:
$(printf '%s\n' "$probe" | sed 's/^/       /')
       Both must be case-sensitive and writable by uid $(id -u). Remove them
       and run again:  docker volume rm $REFS_VOL $OUT_VOL"
fi
info "build volumes: $REFS_VOL, $OUT_VOL (case-sensitive)"

# Say plainly whether the emulation is Rosetta or QEMU. Nobody wants to find
# out four hours into a toolchain build.
emul=$(docker run --rm --platform "$PLATFORM" --entrypoint sh "$IMAGE" \
	-c 'grep -m1 -i "model name\|vendor_id" /proc/cpuinfo 2>/dev/null' 2>/dev/null || true)
case "$emul" in
	*QEMU*|*qemu*)
		warn "emulation is QEMU, which is many times slower than Rosetta."
		warn "Docker Desktop > Settings > General > 'Use Rosetta for x86/amd64"
		warn "emulation on Apple Silicon' makes the toolchain build far quicker."
		;;
	'') ;;
	*) info "emulation: ${emul#*: }" ;;
esac

printf '\n== build ==\n'
printf 'This compiles the cross toolchain, the kernel, the firmware and the\n'
printf 'userspace from source. Under emulation the first run is hours; later\n'
printf 'runs resume from the stage markers in %s and take minutes.\n\n' "${work#$repo/}"
# Docker Desktop presents a bind mount's root as root-owned inside the
# container while the container runs as the host uid, so git's ownership check
# fires on the repos build/container-build.sh clones into refs/ ("detected
# dubious ownership"). On Linux the mount is a real bind mount and the uids
# match, which is why reproduce.sh never needs this. Everything mounted here is
# this project's own working tree, so the check has nothing to protect.
docker rm -f esp32-reproduce-macos >/dev/null 2>&1 || true
docker run --platform "$PLATFORM" ${build_net[@]+"${build_net[@]}"} \
	--name esp32-reproduce-macos --rm \
	--mount "type=bind,source=$work,target=/work" \
	--mount "type=volume,source=$REFS_VOL,target=/work/refs" \
	--mount "type=volume,source=$OUT_VOL,target=/work/$OUT_PATH" \
	--env JOBS="$JOBS" \
	--env GIT_CONFIG_COUNT=1 \
	--env GIT_CONFIG_KEY_0=safe.directory \
	--env GIT_CONFIG_VALUE_0='*' \
	"$IMAGE" 2>&1 |
	tee >(head -c "$LOG_CAP_BYTES" > "$work/logs/build.log")

printf '\nBuild directory: %s\n' "$work"
printf 'Artifacts: %s/artifacts\n' "$work"
printf 'Full log: %s/logs/build.log\n' "$work"
printf 'No board access or push was performed.\n'
