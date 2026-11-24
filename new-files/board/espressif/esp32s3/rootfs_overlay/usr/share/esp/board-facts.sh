# What this board is, read from the kernel. Sourced, never run: it prints
# nothing and sets only esp_-prefixed variables, so a caller takes what it
# wants and leaves the rest.
#
# The login banner and the neofetch command both read the board through here,
# which is the point of the file: two copies of "how do you work out the flash
# size" drift, and then the banner and neofetch disagree about the same board.
#
# Every fact is a file read, and all but one of them is a single read of a
# single-valued file, because this runs from a login shell on a board that can
# be down to a couple of megabytes free, and because /usr/bin/user-shell may
# hand the session to dash. The one exception is the SoC vendor, and the
# comment on it below says why.
#
# Nothing here is a literal that could be measured instead. Who made the board
# and who made the chip both come from the device tree, which is the only thing
# on an ESP32-S3 that says either: there is no board ID EEPROM. The board's own
# file, arch/xtensa/boot/dts/arduino-nano-esp32.dts, sets
#
#	model = "Arduino Nano ESP32";
#	compatible = "arduino,nano-esp32", "espressif,esp32s3";
#
# so `model` names the board and `compatible` names the board and the chip.
# /etc/esp-image-version, which build/package-final.py writes, is the image's
# identity rather than the board's, and is the fallback for the name if this
# kernel was booted with a device tree that has no model property.

# --- the image's own identity -------------------------------------------
esp_board=
esp_version=
esp_commit=
if [ -r /etc/esp-image-version ]; then
	while IFS= read -r esp_line; do
		case $esp_line in
			BOARD=*)   esp_board=${esp_line#BOARD=} ;;
			VERSION=*) esp_version=${esp_line#VERSION=} ;;
			COMMIT=*)  esp_commit=${esp_line#COMMIT=} ;;
		esac
	done < /etc/esp-image-version
fi

# --- who made the board, and who made the chip --------------------------
# The device tree is the only thing on this board that names either maker;
# there is no board ID EEPROM on an ESP32-S3. The board's own file,
# arch/xtensa/boot/dts/arduino-nano-esp32.dts, sets
#
#	model = "Arduino Nano ESP32";
#	compatible = "arduino,nano-esp32", "espressif,esp32s3";
#
# so `model` names the board and `compatible` names the board and the chip.
# Everything below comes out of those two files; the image's own identity
# above is only the fallback for the name, for a kernel booted with a device
# tree that has no model property.
esp_board_vendor=
esp_board_model=
esp_soc_vendor=
esp_dt_model=
if [ -r /sys/firmware/devicetree/base/model ]; then
	# The property is a bare NUL-terminated string, so `read` sets the
	# variable and then returns non-zero at end of file. The value is good.
	read -r esp_dt_model < /sys/firmware/devicetree/base/model 2>/dev/null || :
	case $esp_dt_model in
		*' '*)	# "Arduino Nano ESP32": the maker, then the model name.
			esp_board_vendor=${esp_dt_model%% *}
			esp_board_model=${esp_dt_model#* } ;;
		'')	;;
		*)	esp_board_model=$esp_dt_model ;;
	esac
	[ -n "$esp_dt_model" ] && esp_board=$esp_dt_model
fi

# `compatible` holds two strings separated by NUL, and the second is the
# chip's. No POSIX shell reads a NUL-delimited file: dash's `read` has no -d,
# and bash and dash disagree about what a NUL does to a variable -- bash drops
# it, dash stops there -- so a `read` would name the chip on one shell and not
# the other. This is therefore the one place this file forks: a single `tr`,
# once per login, rather than a fork per field.
if [ -r /sys/firmware/devicetree/base/compatible ]; then
	esp_dt_compat=$(tr '\0' '\n' < /sys/firmware/devicetree/base/compatible 2>/dev/null) || esp_dt_compat=
	# The list runs most-specific first, so the last line is the chip's
	# "vendor,part" and its vendor is what precedes the comma.
	esp_nl='
'
	case $esp_dt_compat in
		*"$esp_nl"*) esp_dt_compat=${esp_dt_compat##*"$esp_nl"} ;;
	esac
	case $esp_dt_compat in
		*,*) esp_soc_vendor=${esp_dt_compat%%,*} ;;
	esac
	unset esp_nl
	# A devicetree vendor prefix is lower case by rule; this one is shown to a
	# person, so it is spelled the way its owner spells it.
	case $esp_soc_vendor in
		espressif) esp_soc_vendor=Espressif ;;
		arduino)   esp_soc_vendor=Arduino ;;
	esac
fi

# --- what the chip is ---------------------------------------------------
# /proc/cpuinfo reports the core as LX7_ESP32_S3_MP and the clock the kernel
# was calibrated at. Both are properties of the silicon, not of the image.
# Note this kernel prints no `processor` lines, so counting cores out of
# cpuinfo does not work here -- `grep -c ^processor` returns 0 on this board.
esp_soc=
esp_mhz=
while IFS= read -r esp_line; do
	case $esp_line in
		'core ID'*) esp_soc=${esp_line#*: } ;;
		'cpu MHz'*) esp_mhz=${esp_line#*: } ;;
	esac
done < /proc/cpuinfo
# Trim the core name to the bare part number, and the clock to whole MHz.
case $esp_soc in
	*ESP32_S3*) esp_soc=ESP32-S3 ;;
	*ESP32_S2*) esp_soc=ESP32-S2 ;;
esac
[ -n "$esp_mhz" ] && esp_mhz=${esp_mhz%%.*}

# --- flash: the sum of the partitions the kernel can see ----------------
# This is what Linux has, which on a 16 MB part comes to 16 MB once the
# factory, linux, rootfs, etc and home partitions are added up. It is not read
# from a chip ID register, because the S3 exposes no flash size to Linux.
esp_flash=0
while read -r esp_dev esp_size esp_name; do
	case $esp_dev in
		mtd[0-9]*) esp_flash=$(( esp_flash + 0x$esp_size )) ;;
	esac
done < /proc/mtd
esp_flash_mib=$(( (esp_flash + 524288) / 1048576 ))

# --- RAM ----------------------------------------------------------------
# MemTotal is what Linux can use, which is less than the 8 MB of PSRAM on the
# package: the firmware on core 0 keeps its own share. Reporting the measured
# number rather than the marketing one is the point. MemAvailable is the one
# worth showing as "used", because it is the kernel's own estimate of what a
# new program could actually get, not free pages.
esp_ram_kb=
esp_ram_mib=
esp_ram_avail_kb=
esp_ram_used_kb=
while IFS= read -r esp_line; do
	case $esp_line in
		MemTotal:*)     esp_ram_kb=${esp_line#*: } ;;
		MemAvailable:*) esp_ram_avail_kb=${esp_line#*: } ;;
	esac
done < /proc/meminfo
# /proc/meminfo pads the number: "MemTotal:           7852 kB". A `read` would
# word-split that away for free, but a field read out of the middle of a loop
# needs a pipe and so a fork. Strip the padding a character at a time instead --
# a few iterations, no fork -- then take the number and drop the " kB".
while case $esp_ram_kb in ' '*) true ;; *) false ;; esac; do
	esp_ram_kb=${esp_ram_kb# }
done
while case $esp_ram_avail_kb in ' '*) true ;; *) false ;; esac; do
	esp_ram_avail_kb=${esp_ram_avail_kb# }
done
esp_ram_kb=${esp_ram_kb%% *}
esp_ram_avail_kb=${esp_ram_avail_kb%% *}
if [ -n "$esp_ram_kb" ]; then
	esp_ram_tenths=$(( (esp_ram_kb * 10 + 512) / 1024 ))
	esp_ram_mib="$(( esp_ram_tenths / 10 )).$(( esp_ram_tenths % 10 ))"
fi
[ -n "$esp_ram_avail_kb" ] && esp_ram_used_kb=$(( esp_ram_kb - esp_ram_avail_kb ))

# --- what the kernel calls itself ---------------------------------------
# `uname -a` is the obvious source, but busybox appends the OS field, which
# this build sets to "GNU/Linux" (CONFIG_UNAME_OSNAME) -- a claim about what
# the board runs that says nothing about the board. The four fields worth
# showing are asked for by name instead, and split by parameter expansion
# rather than by cutting columns: the version field contains spaces, so only
# the first and last words of the line can be found positionally.
esp_uname=$(uname -srvm 2>/dev/null) || esp_uname=
esp_node=$(uname -n 2>/dev/null) || esp_node=
esp_sysname=
esp_tail=
esp_release=
esp_rest=
esp_kernel_version=
esp_machine=
if [ -n "$esp_uname" ]; then
	esp_sysname=${esp_uname%% *}
	esp_tail=${esp_uname#* }
	if [ "$esp_tail" != "$esp_uname" ]; then
		esp_machine=${esp_tail##* }
		esp_release=${esp_tail%% *}
		case $esp_tail in
			*' '*) esp_rest=${esp_tail#* } ;;
		esac
		if [ "$esp_rest" = "$esp_tail" ]; then
			esp_rest=
		else
			case $esp_rest in
				*' '*) esp_kernel_version=${esp_rest% *} ;;
				*) esp_kernel_version= ;;
			esac
		fi
	fi
fi

# --- the physical part, and what it holds -------------------------------
# The package is 16 MB of flash and 8 MB of octal PSRAM, and neither number is
# readable from the chip: the S3 exposes no flash-size or PSRAM-size register to
# Linux. The flash figure below is the sum of the partitions the kernel can see,
# which on a 16 MB part comes to 16 MB once factory, linux, rootfs, etc and home
# are added up -- a measurement that happens to agree with the package, and is
# preferred for that reason. The PSRAM figure is the package's, because the
# measurement does not agree: MemTotal is 7852 kB, about 7.7 MiB, because the
# firmware on core 0 keeps its own share of the same die. Both are wanted, so
# both are here, and the field that shows them says which is which.
esp_psram_mib=8

# --- the running system's own numbers -----------------------------------
# These are the fields a neofetch has that the login banner does not, so they
# live here rather than in the command: the banner and neofetch read the same
# file, and a second parser is a second thing to keep true.

# Processes: every numeric name in /proc is a pid. Read with a builtin loop
# rather than `ls | grep -c`, which is two forks for a count.
esp_procs=0
for esp_pid in /proc/[0-9]*; do
	case $esp_pid in
		/proc/[0-9]*) ;;
		*) continue ;;
	esac
	esp_procs=$(( esp_procs + 1 ))
done

# Load average: three floats, then the running/total counts and the last pid.
# Only the first three are wanted.
esp_load1=
esp_load5=
esp_load15=
if [ -r /proc/loadavg ]; then
	read -r esp_load1 esp_load5 esp_load15 esp_load_rest < /proc/loadavg 2>/dev/null || :
fi

# The terminal the command was run from, as a device. `tty` is a busybox
# applet and prints "not a tty" on a pipe, so a failure is a real answer and
# leaves the field out rather than printing that sentence as if it were one.
esp_tty=$(tty 2>/dev/null) || esp_tty=
case $esp_tty in
	'not a tty'|'') esp_tty= ;;
esac

# Who is asking. `id -un` rather than $USER, which busybox login does set but
# `su` does not update -- so a shell entered with `su alice` would still say
# root. The fork is worth it here because this is the one field that is
# allowed to be wrong about nothing.
esp_user=$(id -un 2>/dev/null) || esp_user=

# --- how long it has been up --------------------------------------------
# /proc/uptime is two floats: seconds since boot, then seconds spent idle. Only
# the first is wanted here; it is truncated to whole seconds so the file
# carries the fact and whoever shows it decides on the wording.
esp_uptime_s=
if [ -r /proc/uptime ]; then
	read -r esp_uptime_frac esp_uptime_idle < /proc/uptime 2>/dev/null || esp_uptime_frac=
	[ -n "$esp_uptime_frac" ] && esp_uptime_s=${esp_uptime_frac%%.*}
fi
