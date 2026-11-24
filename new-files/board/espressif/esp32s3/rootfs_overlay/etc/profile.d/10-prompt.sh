# The prompt: root@arduino:~#
#
# /etc/profile has already set PS1 to a bare '# ' or '$ ' by the time
# profile.d is sourced (it checks [ "$PS1" ] before setting it, so only an
# interactive shell gets one at all), and it sources profile.d afterwards --
# both confirmed on the running board. So this replaces that prompt rather
# than racing it.
#
# The user and hostname are not spelled out. They are read at login, so the
# prompt follows /etc/hostname and cannot drift away from what `hostname`
# prints. Editing /etc/hostname on a running board changes this prompt at the
# next login, and this file does not need touching.
#
# The hostname is read rather than written out on purpose: the image is the
# only thing that knows it is an Arduino Nano ESP32, and a literal here would
# be a second place to keep in step.
#
# The sigil is '#' for root and '$' for anyone else, which is what both
# reference banners do.

# The login shell is bash, and this comment used to say the opposite. It read
# "there is no bash in this image and no dash either", on the reasoning that
# /bin/sh is busybox with CONFIG_SH_IS_HUSH=y. That is true of /bin/sh and it
# is not what logs in: /etc/passwd gives root /usr/bin/user-shell, and
# user-shell execs /bin/bash -l whenever it is executable. It is -- the build
# compiles it (experiments/mmu-poc/programs/build-bash.sh) and the board runs
# it, confirmed with `ls -l /proc/<pid>/exe -> /bin/bash` on both gettys. So
# the BASH_VERSION branch below is live and the POSIX one is the fallback,
# which is the reverse of what this file assumed.
#
# Hush has no \u, \h or \w: it would print them literally, backslashes and
# all. The same facts are therefore gathered with command substitution, which
# both shells understand.
#
# This is a real trade, so it is worth being plain about it: the value is
# fixed for the session. `cd` does not move the prompt, and a hostname changed
# under a running shell does not show up until the next login.

# Colour, and where it is safe to use it.
#
# The serial getty runs with TERM=vt100, which is exactly what a terminal
# emulator on the far end of a cable wants, but the panel's getty runs with
# TERM=linux so fbcon maps the 16-slot palette onto the ST7789. Colour is
# therefore emitted for both -- SGR is SGR -- and skipped only when the output
# is not a terminal at all, which is the case a pipe or a redirect would hit.
#
# The codes are written out rather than taken from `tput`: tput is not in this
# image, and a terminfo lookup at every login is a fork this board does not
# need. 1;32 is bold green -- the same green the Arduino tooling uses.
#
# The escapes are wrapped in \[ and \] when bash is the shell, and left bare
# otherwise, and the difference is not cosmetic. Bash expands \033 to a real
# escape when it *displays* the prompt, but readline measures the prompt
# string *before* that expansion -- so an unwrapped code is counted as the
# eleven printable characters `\033[1;32m`, not as zero. On the panel, which
# is 21 columns at 6x8, that made the first line wrap after 8 characters of
# command while every line after it filled the full 21. Measured on the board:
# 21 - 11 = 10, which is exactly where the first line broke. \[ and \] are
# bash's markers for text that occupies no columns, so the count comes out
# right. 19 digits after `# ` now fit on the first line, which is the whole
# width.
#
# Hush has no such marker -- it would print the brackets literally -- so the
# bare form stays for it. BASH_VERSION is the test rather than a check for
# /bin/bash, because it is the running shell that has to understand the
# brackets, not the one on disk.
esp_reset=
esp_colour=
if [ -t 1 ]; then
	if [ -n "$BASH_VERSION" ]; then
		esp_reset='\[\033[0m\]'
		esp_colour='\[\033[1;32m\]'
	else
		esp_reset='\033[0m'
		esp_colour='\033[1;32m'
	fi
fi

if [ "$(id -u)" -eq 0 ]; then esp_sigil='#'; else esp_sigil='$'; fi

# Two prompts, because the two terminals are not the same terminal.
#
# The panel is 21 columns wide at 6x8, so a full `user@host:~#` plus whatever
# the user types wraps after about a dozen characters of command. On tty1 the
# prompt is therefore just the sigil, which is what a narrow console wants and
# what the reference images do too.
#
# The panel is also why the colour codes above are bracketed: with 21 columns
# and an 11-character miscount, the first line of every command wrapped after
# 8 characters. On the serial line the same miscount went unnoticed for the
# whole life of the image, because 80 - 11 is still wide enough to type into.
#
# The serial line is a real terminal emulator on a modern screen and gets the
# whole prompt. /dev/tty is read rather than $(tty) so the comparison is a
# plain string test with no fork.
case $(readlink /proc/self/fd/0 2>/dev/null) in
	/dev/tty1) PS1="$esp_colour$esp_sigil$esp_reset " ;;
	*) PS1="$esp_colour$(id -un)@$(hostname)$esp_reset:$esp_sigil " ;;
esac
export PS1
unset esp_sigil esp_reset esp_colour
