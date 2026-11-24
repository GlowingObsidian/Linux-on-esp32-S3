#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"

# The self-check below runs process-test on the build host, and process-test
# asserts that an O_CLOEXEC descriptor does not survive exec
# (process-test.c:45). Docker Desktop's Rosetta emulation on Apple Silicon
# does not implement that -- execve there leaves FD_CLOEXEC descriptors open.
# Measured inside this same Docker VM, same image, same probe: "still open"
# under linux/amd64 (Rosetta), "closed, errno 9" under linux/arm64 (native).
# So on an emulated host the self-check fails for a reason that has nothing
# to do with this tree. Probe for it and skip explicitly, rather than
# failing the build or letting a check that did not really run look like it
# passed. The cross-compiled process-test is unaffected and is checked on
# the board by build/test-board.py, which wants "PASS: process suite 10/10".
host_honours_cloexec=1
probe=$(mktemp --suffix=.py)
cat > "$probe" <<'PY'
import os, sys
if len(sys.argv) > 1:
    try:
        os.fstat(int(sys.argv[1]))
    except OSError:
        sys.exit(0)          # exec closed it: this host is correct
    sys.exit(1)              # still open: this host ignores O_CLOEXEC
fd = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
os.execv(sys.executable, [sys.executable, __file__, str(fd)])
PY
python3 "$probe" || host_honours_cloexec=0
rm -f "$probe"

for program in process-test programbench jobq; do
    # jobq spawns (vfork); it must not link the fork backend. The tools do.
    libs=$LIBS
    if [[ "$program" == jobq ]]; then libs=; fi
    cc -std=gnu17 -O2 -Wall -Wextra -Werror "$programs_dir/$program.c" -o "$programs_out/$program-host"
    if [[ "$program" == process-test ]]; then
        if [[ "$host_honours_cloexec" == 1 ]]; then
            "$programs_out/$program-host"
        else
            echo "SKIP: process-test host self-check -- this build host does not" >&2
            echo "      implement O_CLOEXEC across exec, so the check cannot" >&2
            echo "      mean anything here (Rosetta on Apple Silicon is the" >&2
            echo "      known cause). process-test is still cross-compiled, and" >&2
            echo "      build/test-board.py still checks it on the board." >&2
        fi
    fi
    "$CC" $CFLAGS -Oz -flto -std=gnu17 -Wall -Wextra -Werror \
        "$programs_dir/$program.c" $LDFLAGS $libs -o "$programs_out/$program"
    "$prefix-strip" --strip-unneeded -R .xt.prop -R .xt.lit "$programs_out/$program"
    "$prefix-size" "$programs_out/$program"
done

# Refuse a jobq that links libfork or imports fork.
if "$prefix-readelf" -d "$programs_out/jobq" | grep -q libfork; then
    echo "jobq links libfork; it must spawn, not fork" >&2; exit 1
fi
if "$prefix-readelf" --dyn-syms "$programs_out/jobq" | grep -qw "fork"; then
    echo "jobq still imports fork" >&2; exit 1
fi
echo "jobq: no fork import"
