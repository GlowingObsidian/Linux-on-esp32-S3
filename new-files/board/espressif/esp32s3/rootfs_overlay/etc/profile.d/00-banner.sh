# The login splash: what this system is, the terms it is offered under, and
# when you were last here.
#
# It used to carry the board's ASCII art and a tagline under it. Both were cut
# on request, and the art file is gone with them -- the boot logo on the panel
# says which board this is now, and a second copy of it in text was a second
# thing to keep true. What is left is the shape every Unix login has had for
# forty years: a system line, the licence notice, and Last login.
#
# Nothing here is a literal that could be measured instead. The system line is
# assembled from the fields /usr/share/esp/board-facts.sh split out, which
# supplies for the login shell.
#
# POSIX shell, and no forks beyond the three the facts file already makes. The
# board can be down to a couple of megabytes of free memory at login time, and
# this runs before the user has done anything.

# Interactive shells only, and only on a terminal.
case $- in
	*i*) ;;
	*) return 0 ;;
esac
[ -t 1 ] || return 0

# Once per login. `su alice` inherits this and stays quiet; `su - alice` is a
# new login and prints the splash again, which is what a login splash is for.
[ -n "${ESP_BANNER_SHOWN-}" ] && return 0
ESP_BANNER_SHOWN=1
export ESP_BANNER_SHOWN

# --- what the board is ---------------------------------------------------
. /usr/share/esp/board-facts.sh

# --- Last login ---------------------------------------------------------
# There is no wtmp on this system -- busybox has no `last`, and /var/log/wtmp
# does not exist, both checked on the board -- so this keeps its own stamp.
# The clock starts at the epoch and is only set once the network hands out a
# time, so a stamp from before 2020 means the clock was never set and the line
# is dropped rather than print a date that means nothing.
esp_year=
esp_stamp=
esp_now=$(date '+%Y %a %b %e %H:%M:%S %Z %Y' 2>/dev/null) || esp_now=
if [ -n "$esp_now" ]; then
	esp_year=${esp_now%% *}
	esp_stamp=${esp_now#* }
fi
case $esp_year in
	''|19??|200?|201?) esp_stamp= ;;
esac

# --- print --------------------------------------------------------------

printf '\n'

# The system line has the shape `uname -a` has, but is assembled from the
# fields board-facts.sh split out, because `uname -a` on this build appends
# GNU/Linux as the OS. The hostname is spliced in after the sysname, in the
# position `uname -a` puts it, and comes from the kernel rather than from a
# copy of /etc/hostname -- so editing /etc/hostname on the board changes this
# line with no script touched, which is the whole test of the rule.
#
# The machine field is the one exception to "read it, do not write it down".
# `uname -m` prints `xtensa`, which is the architecture family and not the
# part. The kernel knows the part only as a compile-time macro
# (XCHAL_HW_VERSION_NAME in arch/xtensa/variants/esp32s3/include/variant/
# core.h), which is not readable at runtime, so the field is written out here
# as `xtensa-lx7` -- the part alone, with the family not repeated beside it.
# A board that is not an S3 would be wrong on this field and only this field.
if [ -n "$esp_uname" ]; then
	if [ -n "$esp_node" ]; then
		# $esp_tail still carries the machine as its last word, so it is
		# dropped before the model goes on the end. Otherwise the line reads
		# `... xtensa xtensa-lx7`, which is the family twice.
		esp_rest_no_machine=${esp_tail% *}
		printf '%s %s %s %s\n' "$esp_sysname" "$esp_node" \
			"$esp_rest_no_machine" "xtensa-lx7"
	else
		printf '%s\n' "$esp_uname"
	fi
else
	uname -a 2>/dev/null || :
fi

printf '\n'

printf 'This system is built with Buildroot and comes with ABSOLUTELY NO WARRANTY, to the extent permitted by\n'
printf 'applicable law.\n'
printf '\n'

if [ -n "$esp_stamp" ] && [ -n "${HOME-}" ]; then
	esp_login_file=$HOME/.last-login
	if [ "$HOME" = /home/root ]; then
		esp_login_file=/home/.root-last-login
	fi
	if [ -r "$esp_login_file" ]; then
		read -r esp_prev < "$esp_login_file"
		[ -n "$esp_prev" ] && printf 'Last login: %s\n' "$esp_prev"
	fi
	# Keep the root login stamp outside the account's home directory.
	(umask 077; printf '%s\n' "$esp_stamp" > "$esp_login_file") 2>/dev/null || :
fi

printf '\n'

# Everything board-facts.sh set, so a splash cannot leave the login shell
# holding a stale clock or a stale memory figure.
unset esp_line esp_login_file esp_prev esp_year esp_stamp esp_now \
	esp_board esp_version esp_commit \
	esp_soc esp_mhz \
	esp_flash esp_flash_mib esp_dev esp_size esp_name \
	esp_ram_kb esp_ram_avail_kb esp_ram_used_kb esp_ram_mib esp_ram_tenths \
	esp_uname esp_node esp_sysname esp_tail esp_release esp_rest \
	esp_kernel_version esp_machine \
	esp_uptime_s esp_uptime_idle esp_uptime_frac
return 0
