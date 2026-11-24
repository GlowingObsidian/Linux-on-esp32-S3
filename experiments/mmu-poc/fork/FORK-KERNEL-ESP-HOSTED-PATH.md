# The fork kernel's `../../../esp-hosted` path

## Symptom

Builds 9, 10 and 11 all died identically while linking the fork kernel:

```
make[2]: *** No rule to make target '../../../esp-hosted/esp_hosted_ng/esp/esp_driver/esp-idf/components/esp_wifi/lib/esp32s3/libnet80211.a', needed by 'vmlinux.o'.  Stop.
make[1]: *** [.../out/linux-fork/Makefile:1362: vmlinux_o] Error 2
make: *** [Makefile:248: __sub-make] Error 2
```

## Why

`arch/xtensa/Makefile` gets six lines from patch 0073:

```make
libs-$(CONFIG_ESP32_WIFI_BLOB) += $(srctree)/../../../esp-hosted/esp_hosted_ng/esp/esp_driver/esp-idf/components/esp_wifi/lib/esp32s3/libnet80211.a
```

That path is written for **buildroot's** kernel tree, which sits at
`$base/build/linux-7.2.4` under the build driver. Three levels up from there is
`$base`, where `esp-hosted` lives. Correct for that tree.

The fork tree sits at `experiments/mmu-poc/out/linux-fork`. Three levels up from
there is `experiments/`, so the same relative path lands on
`experiments/esp-hosted`, which does not exist.

**The decisive detail: `srctree` is `.` in the fork tree.** Measured with
`make -p -n | grep '^KBUILD_VMLINUX_LIBS\|^srctree'`:

```
KBUILD_VMLINUX_LIBS = ./../../../esp-hosted/.../libnet80211.a ...
srctree = .
```

`Makefile:42` sets `abs_srctree := $(realpath $(dir $(this-makefile)))`, which
collapses to `.` when the tree is its own source root. So the prerequisite is
resolved against **make's working directory**, which is `$kernel_dir`.

## The fix

One symlink, placed at the path make actually resolves:

```bash
ln -sfn "$build_dir/esp-hosted" "$kernel_dir/../../../esp-hosted"
```

which is `/work/Linux-on-esp32-S3/experiments/esp-hosted` in the container.

The earlier attempt put it at `$experiment_dir/../esp-hosted`, which is the same
directory — but it was created *inside* the build's bind mount while the build
was running, and `reproduce-macos.sh:284` rsyncs `--delete` over that tree on
every run, so the snapshot wiped it. It also ran after the snapshot, so it was
present in the container's view but the `--delete` had already happened.

## Verified

In the identical container and mount layout, before launching the build:

```
link: lrwxr-xr-x 1 builder dialout 45 ... /work/Linux-on-esp32-S3/experiments/mmu-poc/out/linux-fork/../../../esp-hosted -> /work/refs/esp32-linux-build/build/esp-hosted
resolves to: /work/refs/esp32-linux-build/build/esp-hosted
ASSERT OK: archive reachable
```

## Why not the alternatives

- **A Makefile variable** (`ESP_BLOB_LIBS`) has to be overridden by every caller,
  and the kernel's top-level Makefile re-execs itself (`Makefile:247-249`,
  `__sub-make`), which is one more place for the override to be lost. Tried in
  build 10; it did not take effect.
- **An absolute path in the patch** would break buildroot's tree, which is the
  one that ships.
- **A mount in `reproduce-macos.sh`** would work but touches the build driver for
  a fork-tree-only problem, and `reproduce.sh` (Linux) would need the same.
