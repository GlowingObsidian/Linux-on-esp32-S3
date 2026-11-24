#!/usr/bin/env bash
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work=$(cd "$repo/.." && pwd)
test "$work" = /work
set -a
source "$repo/build/sources.lock"
set +a
export JOBS=${JOBS:-8}
export KBUILD_BUILD_USER=builder KBUILD_BUILD_HOST=esp32-repro
export SOURCE_DATE_EPOCH="$(cat "$work/source-epoch.txt")"
export KBUILD_BUILD_TIMESTAMP="$(LC_ALL=C date -u -d "@$SOURCE_DATE_EPOCH")" KBUILD_BUILD_VERSION=1
export GIT_AUTHOR_DATE='2026-09-05T00:00:00+00:00' GIT_COMMITTER_DATE='2026-09-05T00:00:00+00:00'
mkdir -p "$work/refs" "$work/logs" "$work/stages" "$work/artifacts"
LOG_CAP_BYTES=${LOG_CAP_BYTES:-50000000}
driver="$work/refs/esp32-linux-build"
base="$driver/build"
br="$base/build-buildroot-arduino_nano_esp32"
exp="$repo/experiments/mmu-poc"

clone_locked() {
    local url=$1 revision=$2 target=$3
    if [[ ! -e "$target" ]]; then
        mkdir -p "$target"
        git -C "$target" init -q
        git -C "$target" remote add origin "$url"
    fi
    test "$(git -C "$target" remote get-url origin)" = "$url"
    if ! git -C "$target" rev-parse --verify HEAD >/dev/null 2>&1; then
        git -C "$target" fetch --depth 1 origin "$revision"
        git -C "$target" checkout --detach FETCH_HEAD
    fi
    # Normally the clone must sit exactly on the locked revision. The
    # esp-hosted clone is the exception: apply-local-changes.sh commits this
    # project's network_adapter patch on top of it, because esp_driver's
    # CMakeLists runs `git reset --hard` on every cmake invocation and only a
    # commit survives that. Upstream never meets the case, because it builds in
    # a fresh mktemp directory each run; a work directory that persists so
    # stages resume will instead find HEAD one commit past the revision and
    # fail here, silently, before the stage prints anything. Allow local
    # commits on top, but nothing else: the locked revision must be an
    # ancestor of HEAD.
    if [ "$(git -C "$target" rev-parse HEAD)" != "$revision" ]; then
        git -C "$target" merge-base --is-ancestor "$revision" HEAD || {
            echo "error: $target is not at $revision, and $revision is not an" >&2
            echo "       ancestor of its HEAD ($(git -C "$target" rev-parse HEAD))." >&2
            echo "       The clone cannot be trusted for this build; remove it and" >&2
            echo "       build again." >&2
            exit 1
        }
    fi
}

stage() {
    local name=$1
    shift
    if [[ -f "$work/stages/$name.done" ]]; then
        echo "RESUME: $name already completed in this build directory"
        return
    fi
    echo "START: $name $(date -u +%FT%TZ)"
    (set -euo pipefail; "$@") 2>&1 | tee >(head -c "$LOG_CAP_BYTES" > "$work/logs/$name.log")
    date -u +%FT%TZ > "$work/stages/$name.done"
    echo "PASS: $name"
}

toolchain() {
    clone_locked https://github.com/jcmvbkbc/esp32-linux-build "$BUILD_DRIVER_REV" "$driver"
    mkdir -p "$base"
    clone_locked https://github.com/jcmvbkbc/xtensa-dynconfig "$DYNCONFIG_REV" "$base/xtensa-dynconfig"
    clone_locked https://github.com/jcmvbkbc/config-esp32s3 "$ESP32_CONFIG_REV" "$base/esp32s3"
    make -C "$base/xtensa-dynconfig" ORIG=1 CONF_DIR="$base" esp32s3.so
    clone_locked https://github.com/jcmvbkbc/crosstool-NG "$CTNG_REV" "$base/crosstool-NG"
    cd "$base/crosstool-NG"
    ./bootstrap
    ./configure --enable-local
    make -j"$JOBS"
    ./ct-ng xtensa-esp32s3-linux-uclibcfdpic
    python3 "$repo/build/pin-toolchain.py" .config
    CT_PREFIX="$PWD/builds" ./ct-ng build
    test -x builds/xtensa-esp32s3-linux-uclibcfdpic/bin/xtensa-esp32s3-linux-uclibcfdpic-gcc
}

espctl() {
    # espctl is this board's own program and it is built here rather than
    # committed: a checked-in Xtensa ELF cannot be reviewed, cannot be diffed,
    # and drifts silently from its source, which is what had happened -- the
    # binary in rootfs_overlay/usr/bin had been there since before the source
    # grew its Nano pin names, and nothing rebuilt it.
    #
    # It runs before base-rootfs because that stage rsyncs the overlay into
    # Buildroot, and the userspace stage installs the same overlay over the
    # cramfs at the end, so the compiled file has to exist before either.
    local tc="$base/crosstool-NG/builds/xtensa-esp32s3-linux-uclibcfdpic/bin/xtensa-esp32s3-linux-uclibcfdpic"
    local src="$repo/new-files/board/espressif/esp32s3/espctl/espctl.c"
    local out="$repo/new-files/board/espressif/esp32s3/rootfs_overlay/usr/bin/espctl"
    test -x "$tc-gcc"
    # The flags are the project's own, from experiments/mmu-poc/programs/env.sh,
    # minus libfork: espctl does not fork, and libfork is not built until the
    # userspace stage, which is later than this.
    "$tc-gcc" -Os -mfdpic -mauto-litpools -fPIC -ffunction-sections -fdata-sections \
        -std=gnu17 -Wall -Wextra -Werror \
        -Wl,--gc-sections,-z,stack-size=65536 \
        "$src" -o "$out"
    "$tc-strip" --strip-unneeded -R .xt.prop -R .xt.lit "$out"
    test -x "$out"
    "$tc-size" "$out"
}

# The hash of the kernel patch series, in lexical order -- which is the order
# Buildroot applies them in. Two callers need it: rootfs_base, to decide whether
# buildroot's kernel package needs a clean tree, and userspace, to decide
# whether the out/linux-fork copy made from that tree has gone stale.
patches_sha256() {
    (cd "$repo/new-files/board/espressif/esp32s3/patches/linux" &&
        sha256sum ./*.patch) | sha256sum | cut -d' ' -f1
}

rootfs_base() {
    clone_locked https://github.com/jcmvbkbc/buildroot "$BUILDROOT_REV" "$base/buildroot"
    install -m 755 "$repo/new-files/toplevel/apply-local-changes.sh" "$driver/apply-local-changes.sh"
    if [[ ! -L "$driver/local-changes" ]]; then ln -s "$repo" "$driver/local-changes"; fi
    test "$(readlink "$driver/local-changes")" = "$repo"
    cd "$driver"
    ./apply-local-changes.sh buildroot
    make -C "$base/buildroot" O="$br" arduino_nano_esp32_defconfig
    "$base/buildroot/utils/config" --file "$br/.config" --set-str TOOLCHAIN_EXTERNAL_PATH "$base/crosstool-NG/builds/xtensa-esp32s3-linux-uclibcfdpic"
    "$base/buildroot/utils/config" --file "$br/.config" --set-str TOOLCHAIN_EXTERNAL_CUSTOM_PREFIX '$(ARCH)-esp32s3-linux-uclibcfdpic'
    "$base/buildroot/utils/config" --file "$br/.config" --undefine PRIMARY_SITE --set-str PRIMARY_SITE 'https://sources.buildroot.net'
    grep -qx 'BR2_PRIMARY_SITE="https://sources.buildroot.net"' "$br/.config"
    "$base/buildroot/utils/config" --file "$br/.config" --set-str WGET 'wget -nd -t 3 --timeout=20'
    grep -qx "sha256  $LINUX_TARBALL_SHA256  linux-$LINUX_VERSION.tar.xz" "$repo/new-files/board/espressif/esp32s3/package-patches/linux/linux.hash"
    grep -qx "BR2_LINUX_KERNEL_CUSTOM_VERSION_VALUE=\"$LINUX_VERSION\"" "$br/.config"

    # Buildroot stamps a package as patched without ever looking at the patch
    # files themselves, so a patch added or edited under patches/linux is
    # silently skipped whenever $br already exists: the kernel links from the
    # previous series and nothing anywhere says so. (reproduce.sh never met
    # this -- it built into a fresh mktemp every run -- but the macOS wrapper
    # deliberately reuses its work directory.) Fingerprint the directory and
    # hand the kernel a clean tree whenever it moves. dirclean drops only the
    # build directory, so the tarball is not fetched again.
    local patch_stamp="$br/.linux-patches.sha256" patch_sha
    patch_sha=$(patches_sha256)
    if [[ -d "$br/build/linux-$LINUX_VERSION" ]] &&
       [[ "$(cat "$patch_stamp" 2>/dev/null || true)" != "$patch_sha" ]]; then
        echo "  patches/linux changed; re-extracting and re-patching linux-$LINUX_VERSION"
        make -C "$base/buildroot" O="$br" linux-dirclean
    fi

    make -C "$base/buildroot" O="$br" BR2_JLEVEL="$JOBS"
    printf '%s\n' "$patch_sha" > "$patch_stamp"
    test -s "$br/images/rootfs.cramfs"
}

blob_libs() {
    # The Wi-Fi blob port links Espressif's closed-source Wi-Fi libraries into
    # the kernel, and those libraries live in the ESP-IDF tree that esp-hosted
    # carries as a submodule. The kernel links in the base-rootfs stage, which
    # runs *before* firmware -- and firmware is where the clone and the
    # submodule init used to happen, so the archives did not exist yet and
    # libs-y had nothing to point at.
    #
    # This stage exists only to put those archives on disk early. It is the
    # clone and the submodule checkout and nothing else: no IDF tools, no
    # Python environment, no build. firmware below finds the clone already
    # there (clone_locked is idempotent) and does the rest.
    #
    # Only the six archives the port links are checked for. A missing one is
    # the failure this stage exists to catch, and catching it here names the
    # cause instead of leaving it to a link error an hour later.
    clone_locked https://github.com/jcmvbkbc/esp-hosted "$ESP_HOSTED_REV" "$base/esp-hosted"
    cd "$base/esp-hosted/esp_hosted_ng/esp/esp_driver"
    git submodule update --init --depth 1
    test "$(git -C esp-idf rev-parse HEAD)" = "$ESP_IDF_REV"

    local libs="$base/esp-hosted/esp_hosted_ng/esp/esp_driver/esp-idf/components"
    local a
    for a in \
        esp_wifi/lib/esp32s3/libnet80211.a \
        esp_wifi/lib/esp32s3/libpp.a \
        esp_wifi/lib/esp32s3/libcore.a \
        esp_wifi/lib/esp32s3/libwapi.a \
        esp_wifi/lib/esp32s3/libespnow.a \
        esp_phy/lib/esp32s3/libphy.a
    do
        test -s "$libs/$a" || {
            echo "error: $libs/$a is missing or empty. The Wi-Fi blob port" >&2
            echo "       links it into the kernel; without it the link fails." >&2
            exit 1
        }
    done
    cd "$repo"
}

firmware() {
    # Nothing below is cheap: the IDF tool install, the Python environment, and
    # a from-scratch `idf.py build` of the whole network_adapter. The stage's
    # `.done` marker only says this work directory has run the stage once; it
    # cannot see that the inputs moved, and it cannot see that they did not, so
    # a --fresh run or a cleared stages/ pays all of it again for a firmware
    # that is already correct. The fingerprint covers the firmware patch, the
    # partition table, the sdkconfig defaults, the network_adapter sources, the
    # pinned ESP-IDF revision and make-images.sh -- so a change to any of them,
    # and only those, gets a rebuild. See firmware/firmware-stamp.sh.
    # FIRMWARE_FORCE=1 skips the guard for the case where the tree is suspect
    # rather than the inputs.
    local fw_stamp="$exp/firmware/firmware-stamp.sh"
    local fw_adapter="$base/esp-hosted/esp_hosted_ng/esp/esp_driver/network_adapter"
    if [[ "${FIRMWARE_FORCE:-0}" != 1 ]] &&
       bash "$fw_stamp" check 2>/dev/null &&
       [[ -f "$work/base-images/network_adapter.bin" ]] &&
       [[ -f "$work/base-images/bootloader.bin" ]] &&
       python3 "$repo/build/test-nano-memory-config.py" "$fw_adapter/sdkconfig" \
           "$fw_adapter/build/config/sdkconfig.h" >/dev/null 2>&1; then
        echo "firmware: inputs unchanged, base-images left as built"
        sha256sum "$work/base-images/network_adapter.bin"
        return
    fi
    if [[ "${FIRMWARE_FORCE:-0}" == 1 ]]; then
        echo "firmware: FIRMWARE_FORCE=1, rebuilding regardless of the fingerprint"
    fi
    clone_locked https://github.com/jcmvbkbc/esp-hosted "$ESP_HOSTED_REV" "$base/esp-hosted"
    cd "$driver"
    ./apply-local-changes.sh esp-hosted
    cd "$base/esp-hosted/esp_hosted_ng/esp/esp_driver"
    git submodule update --init --depth 1
    test "$(git -C esp-idf rev-parse HEAD)" = "$ESP_IDF_REV"

    # The IDF tools and their Python environment are the one part of this build
    # that is not a source tree, and they are the reason this stage used to
    # re-download a gigabyte every run. `idf_tools.py install` fetches the
    # Xtensa toolchain for the S3 and `install-python-env` builds a venv around
    # it; both default to $HOME/.espressif, and $HOME inside the container is
    # not on a mounted volume. Every `docker run` is a fresh container, so
    # anything written there is gone by the next one. Pointing IDF_TOOLS_PATH at
    # the refs volume moves them onto storage that persists, and the two
    # commands then find what they need and do nothing.
    #
    # The refs volume is the right home for it rather than a new one: it is
    # already mounted, already per-work-directory, and already holds the
    # crosstool-NG build (8.4 GB) for exactly the same reason. `idf_tools.py`
    # is happy with an absolute path and creates it if it is missing.
    export IDF_TOOLS_PATH="$base/idf-tools"
    mkdir -p "$IDF_TOOLS_PATH"

    cmake .
    cd esp-idf
    # github 504s every single time. PLS HELP ME :'(
    for attempt in 1 2 3 4 5 6; do
        if [ "$attempt" -ge 3 ]; then
            export IDF_GITHUB_ASSETS=dl.espressif.com/github_assets
        fi
        python3 tools/idf_tools.py --non-interactive install && break
        if [ "$attempt" -ge 6 ]; then
            echo "esp-idf tools did not install after $attempt attempts" >&2
            exit 1
        fi
        sleep $((attempt * 15))
    done
    python3 tools/idf_tools.py --non-interactive install-python-env
    set +u
    source export.sh
    set -u
    cd ../network_adapter
    idf.py set-target esp32s3
    cp sdkconfig.defaults.esp32s3.16m8r sdkconfig
    python3 "$repo/build/test-nano-memory-config.py" sdkconfig
    # Configure first: the adapter's configure step can refresh IDF sources.
    # Then keep live firmware code/literals clear of Linux vectors/guard.
    idf.py reconfigure
    python3 "$repo/build/patch-firmware-vector-reservation.py" \
        ../esp-idf/components/esp_system/ld/esp32s3/sections.ld.in
    idf.py build
    python3 "$repo/build/test-nano-memory-config.py" sdkconfig build/config/sdkconfig.h
    cd "$repo"
    bash make-images.sh "$driver"
    # Copy contents, not attributes. `cp -a` also preserves owner and mode,
    # and Docker Desktop's macOS file sharing refuses some of that ("preserving
    # permissions for ...: Operation not permitted"); GNU cp then exits 1 even
    # though every byte was copied, and set -e turns that into a dead build on
    # this stage's final line -- which is how the first full macOS build of
    # this branch ended, with a complete and correct image set already on disk.
    # Nothing downstream reads these files' owner or mode: package-final.py
    # copies them into artifacts and hashes their contents.
    #
    # The destination is removed first because this work directory persists so
    # stages can resume, unlike upstream's fresh mktemp -d. Without that, a
    # second run would find base-images already present and nest a copy inside
    # it as base-images/images.
    rm -rf "$work/base-images"
    mkdir -p "$work/base-images"
    cp -r images/. "$work/base-images"/
    # Recorded only after the images exist, so a build that dies halfway leaves
    # no stamp and the next run does the work again rather than trusting a
    # firmware that was never finished.
    bash "$fw_stamp" write
}

userspace() {
    mkdir -p "$exp/out/programs" "$exp/out/real-bins"

    # out/linux-fork is build-kernel.sh's copy of buildroot's configured kernel
    # tree, and it is what builds the xipImage this image ships -- buildroot's
    # own kernel build below serves its rootfs, not the board. The copy is made
    # only when it is absent, so on a reused work directory it outlives a change
    # to the board config or to patches/linux and then keeps building the
    # previous kernel. Its own check cannot see that, because the copy is what
    # it compares against. Fingerprint what the copy inherits and drop it when
    # that moves -- both halves matter, since the config selects drivers and the
    # patches add them. The stamp is written before the copy can be made, so a
    # failure later in this stage still leaves it matching what was measured.
    fork_stamp="$br/.linux-fork-inputs.sha256"
    fork_sha=$({ sha256sum < "$br/build/linux-$LINUX_VERSION/.config"; patches_sha256; } \
        | sha256sum | cut -d' ' -f1)
    if [[ -e "$exp/out/linux-fork" ]] &&
       [[ "$(cat "$fork_stamp" 2>/dev/null || true)" != "$fork_sha" ]]; then
        echo "  board config or patches/linux changed; discarding the out/linux-fork copy"
        rm -rf "$exp/out/linux-fork"
    fi
    printf '%s\n' "$fork_sha" > "$fork_stamp"
    clone_locked https://github.com/micropython/micropython "$MICROPYTHON_REV" "$exp/out/micropython-src"
    git -C "$exp/out/micropython-src" submodule update --init --depth 1 lib/berkeley-db-1.xx lib/libffi lib/mbedtls
    bash "$exp/make-test-images.sh" "$base"
    bash "$exp/fork/build-kernel-reclaim.sh"
    bash "$exp/fork/make-image.sh"
    bash "$exp/fork/real/fetch.sh"
    # Each program is guarded by its own fingerprint, so a run that changes one
    # of them rebuilds one of them. `build_program` skips the script entirely
    # when nothing it reads has moved -- the scripts begin with `make clean`
    # and `./configure`, which is what made a single change cost all of them.
    # See experiments/mmu-poc/programs/program-stamp.sh for what each covers.
    program_stamp="$exp/programs/program-stamp.sh"
    build_program() {
        local name=$1
        shift
        if bash "$program_stamp" check "$name" 2>/dev/null; then
            echo "RESUME: program $name already built from these inputs"
            return
        fi
        echo "START: program $name $(date -u +%FT%TZ)"
        "$@"
        bash "$program_stamp" write "$name"
        echo "PASS: program $name"
    }
    build_program dash bash "$exp/fork/real/build.sh" dash
    # Not the `lto` variant. With the pinned crosstool-NG compiler (GCC 14.1.0)
    # a bare `-flto` link of make aborts inside the compiler, every time:
    #     lto1: internal compiler error: in return_token, at opts-common.cc:2158
    # The trigger is the make jobserver, not make's sources: the same link
    # succeeds under `make -j1`, and two files with identical flags link fine
    # under `make -j6`. It is a compiler bug, and not isolated to make either
    # -- bash hit the identical ICE. build-bash.sh carries the real
    # workaround (a single LTO partition, which stops lto-wrapper asking the
    # jobserver for worker slots); make does not need it, because `oz` is the
    # same -Oz without -flto and make is small enough that the difference is
    # invisible against the rootfs limit in
    # experiments/mmu-poc/programs/image-profiles.py.
    build_program make env OPT_VARIANT=oz bash "$exp/fork/real/build.sh" make
    build_program atfork bash "$exp/programs/build-atfork-test.sh"
    build_program micropython bash "$exp/programs/build-micropython.sh"
    # bash and socat both verify the tarballs in out/programs/downloads against
    # shell-tools.sha256 before unpacking, so the fetch has to have run. It is
    # a download, so it is skipped only when the tarballs are already there.
    if [[ ! -f "$exp/out/programs/downloads/bash-5.2.37.tar.gz" ]] ||
       [[ ! -f "$exp/out/programs/downloads/socat-1.8.1.3.tar.bz2" ]]; then
        bash "$exp/programs/fetch-shell-tools.sh"
    fi
    build_program bash bash "$exp/programs/build-bash.sh"
    build_program socat bash "$exp/programs/build-socat.sh"
    build_program netcat bash "$exp/programs/build-netcat.sh"
    # The tarball is a download, not a build: it is cheap and its absence is
    # what the dtach build script would fail on. Fetch it only when missing.
    if [[ ! -f "$exp/out/programs/dtach-b027c27.tar.gz" ]]; then
        curl -fL --retry 2 "https://codeload.github.com/crigler/dtach/tar.gz/$DTACH_REV" -o "$exp/out/programs/dtach-b027c27.tar.gz"
    fi
    build_program dtach bash "$exp/programs/build-dtach.sh"
    build_program process-tools bash "$exp/programs/build-process-tools.sh"
    build_program lscpu bash "$exp/programs/build-lscpu.sh"
    build_program pthread-smp-test bash "$exp/programs/build-pthread-smp-test.sh"
    bash "$exp/programs/build-default-tools.sh"
    bash "$exp/programs/make-image.sh"
    bash "$exp/programs/compact-image.sh"
    # --replace: `artifacts/` is this build's own output directory and a
    # resumed run finds the previous run's rootfs.cramfs already there.
    # `build/reproduce.sh` never hits this because it builds in a fresh
    # mktemp -d every time; the persistent macOS work dir does.
    python3 "$exp/programs/image-profiles.py" build --profile all --replace \
        --output "$work/artifacts/rootfs.cramfs"
    python3 "$exp/programs/test-process-tools.py"
    python3 "$exp/programs/test-strip-sections.py"
    python3 "$exp/fork/test-reclaim.py"
}

package() {
    python3 "$repo/build/package-final.py" "$work"
}

export XTENSA_GNU_CONFIG="$base/xtensa-dynconfig/esp32s3.so"
stage toolchain toolchain
stage espctl espctl
stage blob-libs blob_libs
stage base-rootfs rootfs_base
stage firmware firmware
stage userspace userspace
stage package package
sha256sum "$work/artifacts/linux-esp32s3-native-full.bin"
