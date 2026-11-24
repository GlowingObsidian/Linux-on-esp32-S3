#!/bin/bash
# Equal-work comparison: one active worker, then two independent exec workers.
if [[ ${1:-} == --worker ]]; then
    limit=$2
    count=1
    for ((n=3; n<=limit; n+=2)); do
        prime=1
        for ((d=3; d*d<=n; d+=2)); do
            if ((n%d == 0)); then prime=0; break; fi
        done
        if ((prime)); then ((count+=1)); fi
    done
    printf '%s\n' "$count"
    exit 0
fi

limit=${1:-5000}
rounds=${2:-3}
if [[ ! $limit =~ ^[1-9][0-9]*$ || ! $rounds =~ ^[1-9][0-9]*$ ]] ||
   ((limit<100 || limit>50000 || rounds>20)); then
    echo 'Usage: bash smp-primes.sh [limit 100..50000] [rounds 1..20]'
    exit 1
fi
workdir=$(mktemp -d /tmp/smp-primes.XXXXXX) || exit 1
p1= p2=
cleanup() {
    [[ -z $p1 ]] || kill "$p1" 2>/dev/null
    [[ -z $p2 ]] || kill "$p2" 2>/dev/null
    rm -rf "$workdir"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
stamp() {
    read -r up rest < /proc/uptime
    ticks=$((10#${up%.*} * 100 + 10#${up#*.}))
}
worker() {
    BASH_ENV= /bin/bash --noprofile --norc "$0" --worker "$limit"
}
printf 'Prime limit=%s; %s rounds; two equal jobs per phase\n' "$limit" "$rounds"
for ((round=1; round<=rounds; round++)); do
    stamp; start=$ticks
    worker > "$workdir/a" && worker > "$workdir/b" || exit 1
    stamp; serial=$((ticks-start))
    read -r a < "$workdir/a"; read -r b < "$workdir/b"
    [[ $a == "$b" ]] || { echo 'FAIL: sequential result mismatch'; exit 1; }
    expected=$a

    stamp; start=$ticks
    worker > "$workdir/a" & p1=$!
    worker > "$workdir/b" & p2=$!
    wait "$p1"; s1=$?; p1=
    wait "$p2"; s2=$?; p2=
    stamp; parallel=$((ticks-start))
    read -r a < "$workdir/a"; read -r b < "$workdir/b"
    ((s1==0 && s2==0 && parallel>0)) &&
        [[ $a == "$expected" && $b == "$expected" ]] ||
        { echo 'FAIL: parallel worker/result'; exit 1; }
    speed=$((serial*100/parallel))
    printf 'Round %d: primes/job=%s | sequential=%d.%02ds | parallel=%d.%02ds | speedup=%d.%02dx\n' \
        "$round" "$expected" "$((serial/100))" "$((serial%100))" \
        "$((parallel/100))" "$((parallel%100))" "$((speed/100))" "$((speed%100))"
done
echo 'PASS: all workers returned matching prime counts'
