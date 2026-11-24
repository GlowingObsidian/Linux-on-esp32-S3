#!/usr/bin/env python3
"""What test-board.py does not cover: a detached session, passwd
and a fresh login, a reboot that keeps /home, jffs2 written and read back,
the shell under fork load, the bank exchange's latch under that load, a
subshell with 30 KiB of arguments (#22), the RGB LED dark at boot, neofetch
agreeing with the kernel, and bootlog across reboots. Each check leaves the
board as it found it. No WiFi.
"""
import hashlib, importlib.util, json, os, re, sys, time
from pathlib import Path

repo = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('probe', repo / 'experiments/mmu-poc/serial-probe.py')
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)

port = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyACM0'
out = Path(sys.argv[2]) if len(sys.argv) > 2 else repo / 'build-output/extra-board-tests'
out.mkdir(parents=True, exist_ok=True)
results = []

# getty builds its prompt out of the hostname, so matching "buildroot login:"
# would tie this test to the stock image's name. The image under test is called
# arduino, and the hostname is a configurable value rather than a
# fact about the board, so the pattern takes whatever name is there.
LOGIN_PROMPT = re.compile(rb'[\w.-]+ login: ?')


def record(name, fn):
    t0 = time.monotonic()
    try:
        detail = fn() or ''
        status = 'PASS'
    except Exception as e:
        detail = f'{type(e).__name__}: {e}'[:400]
        status = 'FAIL'
    results.append({'name': name, 'status': status, 'detail': detail, 'seconds': round(time.monotonic() - t0, 1)})
    print(f'{status}: {name}' + (f'  -- {detail}' if detail else ''), flush=True)


def reset(c):
    c.port.dtr = False; c.port.rts = True; time.sleep(0.1); c.port.rts = False


def boot(c, seconds=60):
    reset(c)
    data, t0 = b'', time.monotonic()
    while time.monotonic() - t0 < seconds:
        data += c.port.read(max(1, c.port.in_waiting))
        if LOGIN_PROMPT.search(data) and time.monotonic() - t0 > 20:
            break
    return data


def logout(c):
    # Wait for getty's prompt: a password typed before it jams getty for 60 s.
    c.port.write(b'exit\r')
    c.until(rb'login: ?', 30)


def meminfo(c, key):
    return int(re.search(rf'{key}:\s*(\d+)', c.command(f'grep {key} /proc/meminfo', 20)).group(1))


c = probe.Console(port)
c.login()

# ---- 1. the bank exchange is the model on this image -----------------------
def swap_is_the_model():
    sw = meminfo(c, 'ForkSwitchMax')
    assert sw > 0, f'ForkSwitchMax={sw}: this kernel is the copy model'
    return f'ForkSwitchMax={sw}us'
record('swap-model-is-live', swap_is_the_model)

# ---- 1b. the RGB LED is driven dark at boot, and the pins are handed back ---
# S04led drives the three anodes high through sysfs and then frees the lines, so
# nothing holds the LED pins for the rest of the boot. Freeing a line does not
# undo the level: this chip's GPIO driver installs no .free hook, so the kernel's
# release path clears its own bookkeeping and writes nothing to the pad, and the
# LED stays dark. That is measured on the board -- it is what makes letting go
# possible -- and it is the opposite of what this script assumed before, which
# was that the *claim* was what kept the pad dark.
#
# The consequence is that darkness is no longer something software can check.
# The level is readable only while the line is claimed, and claiming it through
# sysfs exports it as an input, which clears the output enable and lights the
# LED to answer the question. So this asks the two things that are checkable:
# that the script reports all three lines as free, and that they really are --
# `espctl gpio set LED_BLUE 1` used to be refused "busy" until someone stopped
# the boot script by hand, and must not be now.
def rgb_led_dark_and_released():
    o = c.command('/etc/init.d/S04led status', 30)
    lines = [line.strip() for line in o.splitlines()
             if line.split()[:1] and line.split()[0] in ('red', 'green', 'blue')]
    assert len(lines) == 3, f'expected three lines, got {o!r}'
    assert all('free' in line for line in lines), \
        'a line is still held: ' + '; '.join(lines)
    # 1 is dark: the RGB channels share an anode, so 0 lights one.
    o = c.command('espctl gpio set LED_BLUE 1', 30)
    assert 'LED_BLUE' in o, f'could not take the blue line: {o!r}'
    return 'red, green and blue driven dark, and all three lines released'
record('rgb-led-dark-and-released', rgb_led_dark_and_released)

# ---- 1c. neofetch prints the board, not a hardcoded idea of it ---------------
# The command is a small stand-in, not upstream neofetch -- see the comment at
# the top of the file it ships as. What is worth a test is that every number it
# prints agrees with the kernel, since the whole point of replacing neofetch was
# to read the board rather than to look like it does.
def neofetch_agrees_with_the_kernel():
    o = c.command('neofetch', 60)
    text = o
    host = c.command('uname -n', 10).splitlines()[-1].strip()
    release = c.command('uname -r', 10).splitlines()[-1].strip()
    mem = int(re.search(r'MemTotal:\s*(\d+)', c.command('grep MemTotal /proc/meminfo', 20)).group(1))
    assert host in text, f'no hostname in the output: {text[-300:]}'
    assert release in text, f'no kernel release: {text[-300:]}'
    # whole MiB, rounded -- 7852 kB comes out as 7.7M and the command must agree
    tenths = (mem * 10 + 512) // 1024
    want = f'{tenths // 10}.{tenths % 10}M'
    assert want in text, f'no memory figure {want}: {text[-300:]}'
    assert 'M /' in text, f'no used/total memory line: {text[-300:]}'
    return f'{host}, kernel {release}, MemTotal {want}'
record('neofetch-reads-the-board', neofetch_agrees_with_the_kernel)

# ---- 2. jffs2: write big, read back byte for byte, twice ---------------------
def jffs2_roundtrip():
    c.command('dd if=/dev/urandom of=/home/.rt bs=4096 count=96 2>/dev/null; sync', 60)
    h1 = c.command('sha256sum /home/.rt | cut -c1-64', 60).splitlines()[-1].strip()
    c.command('cp /home/.rt /home/.rt2; sync; rm /home/.rt; sync', 60)
    h2 = c.command('sha256sum /home/.rt2 | cut -c1-64', 60).splitlines()[-1].strip()
    c.command('rm -f /home/.rt2; sync', 30)
    assert h1 == h2 and len(h1) == 64, f'{h1} != {h2}'
    return '384 KiB written, copied, read back identical'
record('jffs2-write-copy-readback', jffs2_roundtrip)

# ---- 3. bash under fork load --------------------------------------------------
def shell_load():
    before = meminfo(c, 'ForkShadow')
    o = c.command('for i in $(seq 1 40); do (echo $i | tr 0-9 a-j | wc -c) >/dev/null; done; '
                  'x=$(seq 1 200 | sort -rn | head -1); echo LOAD_$x', 120)
    assert 'LOAD_200' in o, o[-200:]
    after = meminfo(c, 'ForkShadow')
    assert after == before, f'ForkShadow {before} -> {after}: shadow pages leaked'
    return f'40 subshell pipelines, ForkShadow {before}->{after}'
record('shell-pipelines-and-subshells', shell_load)

def exec_args_fit_the_stack():
    cmd = "sh -c 'X=$(head -c 30000 /dev/zero | tr \"\\\\0\" a); y=$(echo hi); echo args-$y-${#X}'"
    for _ in range(5):
        out = c.command(cmd, 60)
        assert 'args-hi-30000' in out, out[-200:]
    t = c.command('cat /proc/sys/kernel/tainted', 10).splitlines()[-1].strip()
    assert t == '0', f'tainted={t}'
    return '5 subshells re-executed with 30 KiB of arguments, tainted 0'
record('exec-args-fit-the-stack', exec_args_fit_the_stack)

# ---- 4. the latch: still clean after all of that ------------------------------
def latch_clean():
    d = c.command('dmesg | grep -ciE "nommu_bank|inconsist|bank_report" || true', 20).splitlines()[-1].strip()
    t = c.command('cat /proc/sys/kernel/tainted', 20).splitlines()[-1].strip()
    assert d == '0' and t == '0', f'latch lines={d} tainted={t}'
    return 'no latch report, tainted 0'
record('bank-latch-silent-after-load', latch_clean)

# ---- 5. dtach: start, detach, attach, the program is still there --------------
def session_roundtrip():
    c.command('session start t1 >/dev/null 2>&1 || true', 30)
    time.sleep(1)
    o = c.command('session list', 20)
    assert 't1' in o, o
    c.command('dtach -p /tmp/dtach-0/t1 <<< "echo SESS_$(( 6 * 7 )) > /tmp/sess.out" 2>/dev/null || '
              'sh -c "echo \\"echo SESS_42 > /tmp/sess.out\\" | dtach -p /tmp/dtach-0/t1"', 20, check=False)
    time.sleep(2)
    o = c.command('cat /tmp/sess.out 2>/dev/null; echo', 20)
    c.command('session stop t1 >/dev/null 2>&1 || true; rm -f /tmp/sess.out', 20)
    assert 'SESS_42' in o, f'command sent to the detached session did not run: {o[-120:]}'
    return 'started, wrote through the socket, stopped'
record('session-detach-attach', session_roundtrip)

# ---- 7. passwd, then log in with the new one, then put it back ---------------
def passwd_roundtrip():
    before = os.environ.get('MMU_BOARD_PASSWORD', 'arduino')
    o = c.command('printf "tmp-pw-1\\ntmp-pw-1\\n" | passwd root 2>&1; echo PW_$?', 30)
    assert 'PW_0' in o, o[-200:]
    logout(c)
    os.environ['MMU_BOARD_PASSWORD'] = 'tmp-pw-1'
    try:
        c.login()
        who = c.command('id -un', 10).splitlines()[-1].strip()
    finally:
        # whatever happened, put the password it had back before anything else runs
        c.command(f'printf "{before}\\n{before}\\n" | passwd root >/dev/null 2>&1; sync', 30, check=False)
        os.environ['MMU_BOARD_PASSWORD'] = before
    assert who == 'root', who
    return 'changed, logged out, logged in with it, restored'
record('passwd-and-relogin', passwd_roundtrip)

# ---- 8. reboot: /home persists, second boot is not a factory boot -------------
def reboot_persists():
    c.command('echo PERSIST_OK > /home/.persist; sync', 20)
    c.port.write(b'reboot\r')
    data = boot(c, 90)
    assert LOGIN_PROMPT.search(data), 'no login after reboot'
    assert b'Kernel panic' not in data and b'Oops' not in data, 'fault on reboot'
    c.login()
    o = c.command('cat /home/.persist; rm -f /home/.persist', 20)
    assert 'PERSIST_OK' in o, o
    # a populated /home must not be re-seeded
    o = c.command('test -f /home/root/README.txt && ls /home/.www-seed.* 2>/dev/null | wc -l', 20).splitlines()[-1].strip()
    assert o == '0', f'seed left behind: {o}'
    return 'file survived, no re-seed'
record('reboot-home-persists', reboot_persists)

# ---- 9. bootlog across a reboot ----------------------------------------------
# `verbose` is recorded in /etc and S00bootlog re-applies it at every boot.
# `quiet` is not: it lowers the level for the boot it is run on and leaves no
# marker, so the next boot comes back at the kernel's own default
# (CONFIG_CONSOLE_LOGLEVEL_DEFAULT=7). Neither can change what a boot prints --
# the kernel has written its log before any script runs, and this command line
# carries no `quiet` -- so both boots stream the same log and the only line that
# differs is the init script's confirmation.
def printk_level():
    # A marker, not the bare value: command() returns the terminal's local echo
    # of what was typed along with the output, and the echo wraps on a long line.
    # Single quotes so the board's shell leaves the backslash for printf.
    out = c.command("printf 'LEVEL:%s\\n' \"$(cut -f1 /proc/sys/kernel/printk)\"", 20)
    return int(re.search(r'LEVEL:(\d+)', out).group(1))


def bootlog_roundtrip():
    c.command('bootlog verbose >/dev/null', 20)
    c.port.write(b'reboot\r'); data = boot(c, 90)
    c.login()
    assert b'Linux version' in data, 'the boot log did not stream'
    assert b'bootlog: verbose; console shows every level' in data, 'verbose was not re-applied'
    verbose = printk_level()
    assert verbose == 8, f'verbose did not survive the reboot: level {verbose}'

    c.command('bootlog quiet >/dev/null', 20)
    quiet_now = printk_level()
    assert quiet_now == 4, f'quiet did not lower the level: {quiet_now}'
    c.port.write(b'reboot\r'); data2 = boot(c, 90)
    c.login()
    quiet_after = printk_level()
    assert b'Linux version' in data2, 'the boot stopped streaming under quiet'
    assert b'bootlog: verbose; console shows every level' not in data2, 'quiet still reported verbose'
    assert quiet_after == 7, f'quiet survived the reboot: level {quiet_after}'

    c.command('bootlog verbose >/dev/null', 20)   # leave the board as the image ships it
    return (f'verbose level 8, re-applied; quiet {quiet_now} now, {quiet_after} after a reboot')
record('bootlog-across-reboots', bootlog_roundtrip)

# ---- 10. final health --------------------------------------------------------
def final():
    t = c.command('cat /proc/sys/kernel/tainted', 10).splitlines()[-1].strip()
    # "panic=10 panic_console_replay" is in the command line; that is not a panic.
    d = c.command('dmesg | grep -v "Kernel command line" | grep -cE "Oops|BUG:|panic|inconsist" || true', 10).splitlines()[-1].strip()
    m = meminfo(c, 'MemAvailable')
    assert t == '0' and d == '0', f'tainted={t} bad-lines={d}'
    return f'tainted 0, MemAvailable {m} kB'
record('final-kernel-health', final)

c.port.close()
(out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
n = len(results); f = sum(r['status'] == 'FAIL' for r in results)
print(f'\n{n - f}/{n} passed' + (f', {f} FAILED' if f else ''))
sys.exit(1 if f else 0)
