#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
source_dir="$programs_out/bash-5.2.37"
(cd "$programs_out/downloads" && sha256sum -c "$programs_dir/shell-tools.sha256")
if [[ ! -d "$source_dir" ]]; then
    tar -xzf "$programs_out/downloads/bash-5.2.37.tar.gz" -C "$programs_out"
fi
apply_program_patch "$source_dir" bash-fork.patch
apply_program_patch "$source_dir" bash-help-cross.patch
apply_program_patch "$source_dir" bash-pid-cache.patch
apply_program_patch "$source_dir" bash-vfork.patch
# Compiles the patched bgp_resize() out of jobs.c into a host fixture.
python3 "$programs_dir/test-bash-pid-cache.py" "$source_dir" "$programs_out/bash-pid-cache-test"
staging="$build_dir/build-buildroot-arduino_nano_esp32/staging"
export CPPFLAGS="-I$staging/usr/include"
# -flto-partition=none is required, not a size tweak. With the pinned
# crosstool-NG GCC 14.1.0, a bare `-flto` link of bash aborts inside the
# compiler every time:
#     lto1: internal compiler error: in return_token, at opts-common.cc:2158
# The trigger is the make jobserver. The identical link succeeds under
# `make -j1`, and fails under `make -j6`; a two-file program with the same
# flags links fine under `make -j6`, so it needs a large object set before it
# shows. lto-wrapper reaches for the jobserver to run its LTRANS jobs, and
# something in that path aborts the option parser.
# A single partition removes the need: lto-wrapper then runs the one job
# itself and never asks the jobserver for slots. Verified on this tree --
# `make -j6` with this flag links, and produces a smaller binary than the
# serial LTO link (1373128 vs 1399196 bytes here), so it costs nothing.
# `-flto=4` does NOT work: jobserver mode takes precedence over an explicit
# partition count. See build/container-build.sh, which hits the same bug on
# the fork/real make.
export CFLAGS="$CFLAGS -Oz -flto -flto-partition=none --sysroot=$staging"
export LDFLAGS="$LDFLAGS --sysroot=$staging -L$staging/usr/lib -Wl,-rpath-link,$staging/usr/lib"
cd "$source_dir"
if [[ -f Makefile ]]; then make clean; fi
bash_cv_getcwd_malloc=yes bash_cv_job_control_missing=present \
bash_cv_sys_named_pipes=present bash_cv_func_sigsetjmp=present \
bash_cv_printf_a_format=yes bash_cv_getenv_redef=no \
ac_cv_func_fork=yes ac_cv_func_fork_works=yes ac_cv_func_vfork=no \
ac_cv_lib_dl_dlopen=no ac_cv_func_dlopen=no ac_cv_func_dlsym=no ac_cv_func_dlclose=no \
CC_FOR_BUILD=cc CFLAGS_FOR_BUILD='-O2 -std=gnu17 -include stdint.h' ./configure --host=xtensa-linux --build="$(sh ./support/config.guess)" \
    --prefix=/usr --bindir=/bin --without-bash-malloc --with-curses \
    --disable-nls --disable-rpath --disable-profiling --enable-separate-helpfiles
make -j"${JOBS:-4}" LOCAL_LDFLAGS= bash
make -C builtins helpdoc
install -m 755 bash "$programs_out/bash.debug"
"$prefix-strip" --strip-unneeded -R .xt.prop -R .xt.lit -o "$programs_out/bash" bash
"$prefix-size" "$programs_out/bash"
"$prefix-readelf" -d "$programs_out/bash" | sed -n '/NEEDED/p'
