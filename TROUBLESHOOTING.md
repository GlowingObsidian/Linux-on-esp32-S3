# Troubleshooting

Symptoms seen on real hardware, with what actually caused them. Most were found
the slow way, so the diagnostic step is included rather than just the fix.

## Boot

### Writes to `/home` crawl once blocks have to be reclaimed

Known limitation of the platform, not of this branch. Writing into clean jffs2
blocks is normal; once the filesystem has to reclaim and erase used blocks,
throughput collapses and a 2.25 MiB write does not finish in ten minutes. The
published 0.6 kernel behaves the same, so this is not new here. Measurements
are in [the erase report](build/verification/2026-09-06-jffs2-erase.md).

Boot no longer depends on it: `S06home-users` gives `home-init` a bounded 45
seconds and continues either way. Keep `/home` well under its capacity and
treat large rewrites as something to avoid.

Recover by rewriting the factory `/home`:

```sh
esptool --chip esp32s3 --port YOUR_COM_ADAPTER \
    --before default_reset --after no_reset \
    write_flash 0xb40000 ARTIFACTS/home.jffs2
```

That erases whatever was in `/home`. Treat the partition as scratch space.

### First boot stops after `Running sysctl: OK`, or panics there

On an image before 2026-09-14 this was the flash cache: the firmware never
invalidated it after a write Linux made, so `home-init`'s burst of jffs2
writes in exactly this window left the kernel reading stale pages -- a
`Kernel panic - not syncing: BUG!` with nothing before it, a silent hang, or a
script in `/etc` that suddenly has a syntax error. DEVELOPMENT.md incident 15
has the trail and the fix; reflash the firmware. On a current image, read on.

The next line of a healthy boot is `Starting network (background): OK`. If it
stops at `sysctl`, an init script is blocked writing to `/home`. Add `set -x`
to `/etc/init.d/rcS` and to the script on a test board to find the exact line.
The original failure traced to `mkdir -p /home/root` in `S05home`; that script
no longer writes, `home-init` creates the directory instead and `S06home-users`
bounds it, so init continues even when the write is slow.

A fully erased `home` partition carries no JFFS2 cleanmarkers. In the observed
failure, the first `mkdir` stalled and login was not reached within four
minutes. Repackaging with an empty formatted partition removed that stall;
the subsequent clean build reached login in 19.08 seconds. This isolates the
packaging condition but does not prove that every first allocation must erase
every block of the partition, or establish the exact interaction with execution
from flash.

Check the image rather than the scripts:

```sh
python3 -c "d=open('linux-esp32s3-native-full.bin','rb').read()[0xb40000:0x1000000]
print(d[:12].hex(), d.count(b'\xff'), len(d))"
```

A factory `/home` starts every 64 KiB block with `851903200c000000`. All `ff`
means the image was packaged without `home.jffs2`; rebuild it with
`make-images.sh`, or write that file at the `home` offset. The command
`flash.sh --parts --erase` needs the separate file as well, because erasing
removes the formatting.
A combined image already includes its own `/home` bytes and overwrites them
even without `--erase`. Earlier
releases packaged the partition erased too; whether they stall for as long
has not been measured here.

### First boot reports `tar: invalid option -- z` and `/home/www` is missing

This belongs to images up to 0.9.1, which seeded an editable web page into
`/home`. An early experimental image used `tar -xzf`, but its BusyBox
configuration did not enable gzip handling inside tar; the corrected
`home-init` used `gzip -dc` followed by plain `tar -xf`, checked decompression
failure and cleaned its temporary directory.

There is nothing to unpack on the current branch. The web server is gone, so
`home-init` no longer seeds `/home/www` and `/usr/share/esp32-home` no longer
carries a `www.tar.gz`. If you are on an image that still prints this, either
flash a current one or fix `/home` with `flash.sh --parts`.

### Boot stops after `mmc_spi`, no panic, no further output

The next line in a healthy boot is `esp32s3-rsa: selftest 512-bit PASS`. If it
never appears, the kernel is spinning in the RSA driver's opening wait:

```c
while (readl(rsa + RSA_QUERY_CLEAN) != 1)
        cpu_relax();
```

That loop has **no timeout**. It waits for the RSA block to be clocked, and the
driver never clocks it on purpose — the SYSTEM clock registers are shared with
the firmware's AES/SHA, so the firmware hands the block over instead, with
`periph_module_enable(PERIPH_RSA_MODULE)` in `app_main.c`.

So a kernel paired with a firmware that lacks that call hangs here, silently.
Fixed in the firmware patch; if you hit it, your firmware is older than your
kernel. Rebuild both from the same checkout.

### Boot stops right at `Run /sbin/init`, having printed everything else

`CONFIG_VECTORS_ADDR` in the kernel config does not match `space_for_vectors` in
the firmware ELF. Linux writes its exception vectors over firmware memory and
dies the instant it executes userspace.

The address is chosen by the firmware's linker, so it **moves whenever the
firmware's size changes**. `make-images.sh` now detects this, realigns the
kernel config and rebuilds — it should not reach you. If you build by hand,
compare them yourself:

```sh
xtensa-esp32s3-elf-nm network_adapter.elf | grep space_for_vectors
grep CONFIG_VECTORS_ADDR .../nano_esp32_linux.config
```

### Guru Meditation boot loop, before Linux prints anything

`CONFIG_KERNEL_LOAD_ADDRESS` must equal `0x42000000 +` the `linux` partition
offset. Moving a partition without updating it produces exactly this.

### `jffs2: Name CRC failed on node at 0x...`

One line during boot, and then everything works normally. A directory entry on
`/etc` or `/home` was written only half way; jffs2 notices during the scan it
does at mount, discards that entry and carries on. The scan exists for exactly
this — it is the filesystem repairing itself, not reporting a fault.

Which of the two partitions it is: the offset is relative to the start of the
partition, so compare it against `cat /proc/mtd`. Anything past `etc`'s
`0x70000` can only be `home`.

Nothing important can be lost this way. The base system is a read-only cramfs
and cannot be touched; what goes missing is whatever that one entry named,
under `/etc` or `/home`. In the case seen here, an empty directory.

It comes from a write cut short — a flash interrupted part way through, or the
power going while something was being saved. Clear it for good by erasing
first:

```sh
./flash.sh --erase
```

Verified: present after an interrupted flash, gone after a full erase and
rewrite of the same image.

### The boot prints almost nothing

`quiet` on the kernel command line holds the console at KERN_ERR and worse.
This image does not set it: patches 0049 and 0060 leave the bootargs without
`quiet`, so the console sits at `CONFIG_CONSOLE_LOGLEVEL_DEFAULT=7` and the
boot streams live, down to KERN_INFO, as it happens. `/etc/bootlog/verbose`
ships present, which raises it to 8 so KERN_DEBUG comes through too. An image
that does print almost nothing is one still carrying `quiet`; it saves about
0.7 s of a 13.7 s boot on a 115200 line, and everything is still in the ring
buffer -- `dmesg` shows it, and `bootlog verbose` puts it back on the console
at boot.

`bootlog quiet` is not the way to get that back. It lowers the console level
from the moment it runs, so it holds nothing back that has already been written
and the boot you are on has usually finished printing. It leaves no marker, so
the next boot starts from the kernel's own default again. A quiet boot is a
command line with `quiet` on it, which means rebuilding the device tree, not a
command on the board.

Neither covers a fault before init runs, about 1.4 s in. For those,
`panic_console_replay` dumps the buffer when the kernel panics.

### Opening the serial console resets the board

On the CH34x adapter DTR and RTS are wired to EN and IO0, so most terminal
programs reset the board when they open the port. That is a fresh boot and a
fresh WiFi association: for a few seconds after any console session the
board answers no ping and no SSH. It is not a fault. The project's
`serial-probe.py` lowers both lines before opening to avoid it.

### The USB port shows nothing after the ROM's boot messages

That is expected. The chip has one USB PHY and this image gives it to the USB
host controller once Linux starts, so the port is not a console: `ttyGS3` is
not created, and `usb-console` no longer exists to offer a login on it. The
console is UART0 on D0/D1, 115200 8N1. Flashing still uses the USB port,
because the ROM takes the PHY back while the chip is in download mode.

### Nothing happens when I plug something into the USB port

The port is a host, and it powers nothing: no VBUS regulator is described in
the device tree, so the device needs its own supply — a powered hub's input is
what that is for. Given power, work outwards from the bus:

- `ls /sys/bus/usb/devices/` — a `1-1` entry means it enumerated and took a
  bus address. No entry at all is a wiring, power or PHY problem, not a driver
  one.
- `ls -l /sys/bus/usb/devices/1-1*/driver` — the symlink names the driver that
  claimed it. `(none)` means it enumerated and nothing bound to it.
- `cat /proc/bus/input/devices` — a keyboard or mouse appears here with
  `Handlers=... eventN`. `cat /dev/input/event0 | hexdump -C` then prints a
  record per keypress, 16 bytes each on this 32-bit kernel: time, type, code,
  value.
- `cat /proc/interrupts` — the USB line's count climbing while the device is
  used rules out a stale enumeration.
- `dmesg | grep -i usb` — `dwc2` prints the device it found, including its
  vendor and product IDs.

There is no serial class driver in this image, so a USB-serial adapter will
enumerate and bind to nothing. A device that enumerates and then resets itself,
or drops off under load, is usually a power problem rather than a driver one.

A flash drive or card reader is the exception — it binds to `usb-storage` and
becomes `/dev/sda`. If it enumerates but no `/dev/sda` appears, the class driver
did not bind; if `/dev/sda` appears but `mount` says `unknown filesystem type`,
the drive is exFAT or NTFS, which this build does not carry. Only FAT is
understood.

### `scp: Connection closed` right after connecting

OpenSSH 9 copies over SFTP by default and dropbear ships no `sftp-server`.
`scp -O file root@board:` uses the old protocol and works.

### A name will not resolve but others do

Ask the router directly and read the answer code before blaming the board:
`nslookup name ROUTER`. Some routers and ISP resolvers answer NXDOMAIN for
particular names -- `example.com` is one such on at least one home router --
while resolving everything else. The board, its driver and its libc do what
the reply says.

## After boot

### The prompt is `~` but you are in `/root`, and writes fail

`/root` is on the read-only cramfs. Root's home should be `/home/root`, on the
writable jffs2 partition. If it is not, `setup-home.sh` did not run — check that
it is listed in `BR2_ROOTFS_POST_BUILD_SCRIPT` in the defconfig.

```sh
grep ^root: /etc/passwd     # should end in /home/root
```

### The board joined a WiFi network you never configured

See [SECURITY.md](SECURITY.md#past-incidents). Check for a network block with no
`ssid` in `/etc/wpa_supplicant.conf`; that matches any open network. Delete the
file and reboot.

### `Command[4] timed out` / `cmd_scan_request ... ret: -22`

A scan request the firmware did not answer. Occasional ones while the firmware
is busy are survivable — `wpa_supplicant` retries and eventually associates.

Continuous ones that never recover used to mean the SoftAP had been brought up,
which wedged the firmware so the STA could no longer scan. The SoftAP was
disabled in 0.3 and the code removed from both the driver and the firmware
afterwards, so this should not happen; if it does, it is worth an issue.

### `curl: (60) peer certificate could not be verified` on every HTTPS site

The clock, not the CA bundle. **The board has no RTC, so it powers on at 1 Jan
1970, every time.** Every certificate's `notBefore` is decades in the future
from there, so verification fails — for *all* sites, which is what tells this
apart from a root that is genuinely missing from the trimmed bundle.

Check first:

```sh
date                                       # 1970 means NTP has not run
grep clock /var/log/network.log            # what it tried, and when
```

The clock is set over NTP from a udhcpc hook, so it needs a DHCP lease *and*
working internet. If it is still 1970:

- **No internet, only a LAN.** Nothing to ask. Set it by hand.
- **UDP 123 filtered.** Some networks block it. Set it by hand.
- **DNS broken.** Two of the three peers are IP literals precisely so this does
  not matter, but if outbound UDP is blocked too, the same applies.

```sh
date -s "2026-07-31 03:30:00"
curl -sI https://github.com | head -1      # HTTP/1.1 200 OK
```

Verified on hardware: with the clock at 1970 github, google and example.com all
fail; with it set, all three succeed.

The board now remembers the time across a reboot: `S01clock` writes it to
`/home/.clock` at shutdown and restores it at boot when it is newer than what
the clock says. That is the time the board was last running, not the real one,
but it is on the right side of every certificate date, so TLS works before NTP
answers. A board that lost power without a clean shutdown, or that has never
had the right time, still starts in 1970.

`CURL_CA_BUNDLE` is not what decides this. The bundle path is compiled into
libcurl with `--with-ca-bundle`, so curl finds it with the variable unset and
in contexts a profile script never reaches — inetd, a detached session. If TLS is
failing, the clock is the thing to check.

### `sh: bad number` after almost every command

This section records the earlier 0.6 BusyBox-login behavior. The current
branch uses Bash for user logins and fixes hush's recursive profile loading
during NOMMU re-execution. Persistent errors, growing chains of `sh`, or OOM
in a current image are not expected: check the image/kernel version and run
`/usr/bin/dash /usr/share/program-tests/hush-login-test.sh`.

In the older **interactive** shell every command
substitution prints it — one to three times, unpredictably — while producing
the right value:

```sh
A=$(echo hi); echo "A=[$A]"
sh: bad number
sh: bad number
sh: bad number
A=[hi]
```

`$(echo hi)` runs no external program at all, so this is hush itself: on a
older NOMMU image it cannot fork, and re-executes busybox to run a subshell. The
message comes out of that path.

It is confined to shells whose stderr is a terminal. Scripts are unaffected —
the boot log and `/var/log/network.log` are clean, and the substituted values
are always correct. Ignore it. If it bothers you, redirect: `cmd 2>/dev/null`.

## Native userspace

### The shell switched itself to dash, or a switched board will not log in

With WiFi associated there may not be enough memory left for Bash to fork, and
every external command fails with `fork: Cannot allocate memory`. An
interactive Bash login installs a hook that notices this, switches to dash,
prints why, and retries the command. The choice is remembered in
`~/.shell`, so it survives a reboot.

Go back with `use-shell bash`, then `exec /bin/bash -l`. If the board will not
give you a usable shell at all, rewrite the two writable partitions, which
clears `~/.shell` along with everything else in `/home`:

```sh
./run.sh --recover
```

Verified on the board: a second login shell with WiFi up failed to fork at
772 KiB, the hook switched to dash, printed why, and re-ran the command that
had just failed.

The switch happens below 900 KiB of `MemAvailable`. That number is measured,
not estimated: a second login shell on a board with WiFi up failed to fork at
740 KiB, while the same board forks normally at around 1330 KiB. An earlier
700 KiB threshold sat just under the real failure point and never fired.

`MemAvailable` counts reclaimable cache, but a fork here needs real free pages
for its private bank, so the shell can fail well above what that number
suggests. Adjust `LOWMEM_THRESHOLD` in the same file if your board differs.


### Fork fails or processes are killed with several consoles open

Fork support does not add RAM. In diagnostic tests, two interactive Bash
consoles left too little memory for `id`; a Bash primary console with two
detached Dash sessions could also fail while launching `session list`.
For the tested multi-session setup, temporarily replace the primary console
with `exec /usr/bin/dash -i`, and return with `exec /bin/bash -l` afterwards.
Run additional background jobs separately. `jobq` can limit admission for
its own jobs, but cannot reserve memory against unrelated processes.

The instrumented benchmark suite likewise replaces the primary Bash rather
than keeping another shell alive. Functional program tests still run from
Bash. See [the verification conditions](build/README.md#hardware-tests);
passing these bounded tests does not imply arbitrary concurrency is safe.

### `find` or `du` over `/sys` eats memory and never gives it back

Walking a directory tree allocates a dentry and an inode per node, and on this
kernel those are freed only by the superblock shrinker, which `SLUB_TINY`
leaves with nothing reclaimable to work on. Measured on the board:

```
before:           Slab 2128 kB   MemAvailable 3112 kB
find /sys -name 'fork_bank*' >/dev/null
after:            Slab 3748 kB   MemAvailable 1428 kB
find /home -name 'nosuchfile' >/dev/null
after:            Slab 3748 kB   MemAvailable 1428 kB
```

1620 kB for the walk of `/sys`, and it does not come back until a reboot. A
second `cat /proc/meminfo` reads the same number, so it is not drift.

The cost scales with how many nodes the walk visits, and only a tree with
thousands of nodes shows it. The same `find` over `/home` cost nothing at all
— its dentries fit in slab pages that already exist. `/sys` is the tree on
this board that is large enough to matter, so it is the one to avoid.

If the board is short of memory and something walked `/sys`, that is the
cause, not a fork leak and not the USB stack.

There is no fix from userspace: the objects are kernel slab, and the
allocator variant that would let the shrinker take them back is the one that
costs 2 MB of slab at boot. Avoid the walk, or reboot.

## Build

Build failures have their own issue template, which asks for what is actually
needed to diagnose one. See also the incidents list in
[DEVELOPMENT.md](DEVELOPMENT.md) — every entry there was a build that reported
success and produced something wrong.

### `lto1: internal compiler error: in return_token` linking a userspace program

```
during IPA pass: static-var
lto1: internal compiler error: in return_token, at opts-common.cc:2158
lto-wrapper: fatal error: .../xtensa-esp32s3-linux-uclibcfdpic-gcc returned 1 exit status
ld: error: lto-wrapper failed
make: *** [Makefile:595: bash] Error 1
```

A bug in the pinned crosstool-NG compiler (GCC 14.1.0), not in the package
being built, and it is the **make jobserver** that triggers it. A bare `-flto`
link of a large object set aborts inside the compiler while lto-wrapper runs
its LTRANS jobs through the jobserver that `make -j` creates. A two-file
program with the same flags links fine, so it looks source-specific and is not;
it needs a large object set before it shows. `bash` and `make` have both hit
it, and `socat` would.

Every row below is the same object set, in
`experiments/mmu-poc/out/programs/bash-5.2.37`:

| link | result |
|---|---|
| `make -j6` | ICE |
| `make -j1` | links — "using serial compilation of 16 LTRANS jobs" |
| `make -j6`, `-flto=4` | ICE — jobserver mode wins over an explicit partition count |
| `make -j6`, `-flto -flto-partition=none` | links |

The fix is the last row. A single partition means lto-wrapper runs the one job
itself and never asks the jobserver for slots. It costs nothing, and if
anything helps: the link is slightly *smaller* than the serial LTO link
(1,373,128 vs 1,399,196 bytes for bash). `build-bash.sh` and `build-socat.sh`
carry the flag; `fork/real/build.sh`'s `lto` variant does not, so
`build/container-build.sh` builds make with `OPT_VARIANT=oz` instead.

Dropping `-flto` at the **link** does not help. Each object records its
compile-time options in `.gnu.lto_.opts` and the linker's LTO plugin replays
them, so the objects must be recompiled to actually leave LTO behind.

`build-process-tools.sh` also passes `-flto`, and is fine: it invokes `$CC`
directly rather than through make, so there is no jobserver.

### A build log looks frozen for minutes at a time

The on-disk copy of each stage log is written by
`build/container-build.sh` as `tee >(head -c "$LOG_CAP_BYTES" > ...)`. GNU
`head` buffers its output, so the file advances a 4096-byte block at a time
rather than streaming, and `tail -f` on it lags the real build by up to one
block. A log parked at a size divisible by 4096 — 196608, say — is at a buffer
boundary, not stalled.

Read the build's health from the container, not the file:

```
docker stats --no-stream esp32-reproduce-macos
docker top esp32-reproduce-macos
```

Zero CPU with all processes in state `S` is expected while a toolchain
downloads, which the `firmware` stage does for about ten minutes on every run:
ESP-IDF installs its tools into `$HOME/.espressif`, which is inside the
container and not in a Docker volume, so `idf_tools.py install --targets=all`
re-fetches them each time.

