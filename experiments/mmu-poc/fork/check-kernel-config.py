#!/usr/bin/env python3
"""Assert a built .config still carries every statement of the seed config.

build-kernel.sh copies the configured buildroot tree into out/linux-fork only
when that directory does not exist, and the directory is gitignored, so it
outlives everything. A change to the seed config can therefore be added, built,
measured and released without ever having reached the kernel, and the build
succeeds all the way through. Compare the two and fail loudly instead.

The comparison is one-directional: only symbols the seed mentions are checked.
build-kernel.sh sets XTENSA_NOMMU_FORK and LOCALVERSION behind the seed's back
and neither is named in the seed, so neither is flagged. Naming either one in
the seed would fail every build.
"""
import re
import argparse
import sys
from pathlib import Path

SET = re.compile(r'^(CONFIG_[A-Z0-9_]+)=(.*)$')
UNSET = re.compile(r'^# (CONFIG_[A-Z0-9_]+) is not set$')


def statements(text):
    result = {}
    for line in text.splitlines():
        match = SET.match(line)
        if match:
            result[match[1]] = match[2]
            continue
        match = UNSET.match(line)
        if match:
            result[match[1]] = None
    return result


def compare(seed_path, built_path):
    seed = statements(Path(seed_path).read_text())
    built = statements(Path(built_path).read_text())
    divergences = []
    for symbol, wanted in seed.items():
        present = symbol in built
        got = built.get(symbol, 'absent')
        if wanted is None:
            # olddefconfig drops symbols whose dependencies are unmet, so an
            # absent symbol is disabled just as surely as an explicit "not set".
            if present and got is not None:
                divergences.append((symbol, wanted, got))
        elif not present or got != wanted:
            divergences.append((symbol, wanted, got))
    return divergences


def show(value):
    return 'not set' if value is None else 'absent' if value == 'absent' else value


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-fork', action='store_true',
                        help='reject a kernel incompatible with the forkbank rootfs')
    parser.add_argument('seed')
    parser.add_argument('built')
    args = parser.parse_args()
    found = compare(args.seed, args.built)
    if args.require_fork:
        built = statements(Path(args.built).read_text())
        if built.get('CONFIG_XTENSA_NOMMU_FORK') != 'y':
            print('Forkbank rootfs requires CONFIG_XTENSA_NOMMU_FORK=y. '
                  'Kconfig may have disabled it because its dependencies are unmet; '
                  'do not package or flash this kernel with libfork/Bash.', file=sys.stderr)
            raise SystemExit(1)
    if not found:
        print(f'{args.built}: matches every statement in {args.seed}')
        raise SystemExit(0)
    print(f'{args.built} does not carry {len(found)} statement(s) from {args.seed}:',
          file=sys.stderr)
    for symbol, wanted, got in found:
        print(f'  {symbol}: seed says {show(wanted)}, built has {show(got)}', file=sys.stderr)
    print('\nThe built tree is older than the board config. out/linux-fork is a copy'
          '\nof buildroot\'s kernel tree, taken only when it does not already exist, so'
          '\nboth of them can be the stale one:'
          '\n  rm -rf experiments/mmu-poc/out/linux-fork    # the copy'
          '\nand if that still reports the same symbols, buildroot\'s own tree predates'
          '\nthe config -- rebuild it (build/reproduce.sh); removing the copy cannot'
          '\nfix what it is copied from.', file=sys.stderr)
    raise SystemExit(1)
