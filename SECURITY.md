# Security

This is a research project: a general-purpose Linux running on a microcontroller
that was never designed to host one. It is meant for a workbench and a trusted
home LAN. **Do not put a board running this image on an untrusted network, or
anywhere the network it joins matters.**

Nothing below is a bug report. These are deliberate trade-offs, listed so the
choice to accept them is yours and not a surprise.

## What the shipped image exposes

The default-credential/network warnings apply to both the older images and
the current branch. The new userspace adds private homes, `su`/`passwd` and
BusyBox is SUID root for the applets that require it; ordinary
applets drop privileges. There is no sudo/doas or automatic wheel-group
elevation.

**These are Unix permission checks, not hardware memory isolation.** The
NOMMU fork backend does not make untrusted native code safe. Use only trusted
users/programs; do not expose shells to hostile users.
Raw flash backups can contain passwords, WiFi configuration and user files;
keep them private and separate from distributable build artifacts.

| | |
|---|---|
| **Root password** | `arduino` on every flashed board. Nothing prompts a change — set your own with `passwd` before putting the board on a network you do not control |
| **Telnet** | not in this image. It sent the password in clear text, so it was removed; SSH is the only way in |
| **SSH** | off until you turn it on with `remote-login ssh`; port 22, password or key until `remote-login port`/`auth` say otherwise; slow on this hardware |
| **Firewall** | none. No firewall package ships in the image |
| **Secure boot / flash encryption** | not used; flash can be read and rewritten over USB |

Nothing filters inbound traffic: a port is closed only because no process is
listening on it. `iptables` used to ship with an empty ruleset that changed
nothing until you wrote rules; 0.8 removed it entirely to reclaim flash (see
the changelog), and there is no other firewall tool on the image.

The password is a word, not a secret: `arduino` on every board this project
builds, and nothing prompts you to change it. That is the trade — a documented
default is honest about what it is, where a plausible-looking one invites you
to leave it in place believing it is yours. Either way it is public, so run
`passwd` before the board goes anywhere that matters. Nothing listens on the
network until something is listening: a board joined to WiFi is not
reachable until `remote-login ssh` turns a way in on, and
`remote-login off` closes it again. `ssh-server on` is the same switch under
another name.
`remote-login auth key` turns SSH passwords off once a key is in
`/home/root/.ssh/authorized_keys`, which is the setting to use on any network
you do not fully trust; a different port only keeps the log quieter.

### It asks the internet what time it is

Every time the board joins a network it sends an NTP query to `pool.ntp.org`
and to two IP literals belonging to Cloudflare and Google. That is
unsolicited outbound traffic to third parties, and the answer is
unauthenticated — plain NTP has no signatures, so whoever can intercept it can
set this board's clock to whatever they like, which in turn decides whether an
expired certificate looks valid.

It is the same trade every appliance without a battery makes, and the
alternative is a board that cannot do HTTPS at all until someone types the
date. If you would rather it stayed quiet, delete
`/usr/share/udhcpc/default.script.d/50-set-clock` — but it is on the read-only
cramfs, so in practice that means rebuilding the image.

## Past incidents

**Any open WiFi network, joined automatically.** Buildroot's `wpa_supplicant`
package installs its own `/etc/wpa_supplicant.conf` containing a network block
with no `ssid` and `key_mgmt=NONE`, which matches *any* unencrypted network. A
board built from this repo joined a carrier hotspot on its own, unprompted.

The released images never carried the file, but only by accident — it had been
deleted by hand from an incremental build tree, and nothing here reproduced
that. Fixed by removing it in a post-build script, plus a check in
`make-images.sh`, whose previous check looked only for a `psk=` line and so
missed it entirely: **an open-network block contains no credentials at all,
which is exactly what makes it dangerous.**

If you built from source before that fix, check the board:

```sh
cat /etc/wpa_supplicant.conf     # a network block with no ssid is the bad one
```

## Reporting something

Open a GitHub issue. There is no private disclosure channel — this is a
single-maintainer hobby project, and pretending otherwise would be worse than
saying so. If a finding genuinely should not be public first, open an issue
asking for a contact and leave the details out of it.

No support commitment, no fix timeline, no backports. The reproducibility
caveats and the list of past build defects are in
[DEVELOPMENT.md](DEVELOPMENT.md).
