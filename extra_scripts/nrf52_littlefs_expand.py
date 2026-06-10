#!/usr/bin/env python3
# trunk-ignore-all(ruff/F821)
# trunk-ignore-all(flake8/F821): For SConstruct imports
#
# Expand the nRF52840 internal LittleFS partition 28 KB -> 40 KB.
#
# The partition address/size are hard-coded in the Adafruit framework
# (InternalFileSytem/src/InternalFileSystem.cpp, no #ifndef override), so this
# pre-script patches the package source in place, idempotently, before each
# build:
#
#   stock:    7 pages at 0xED000 (0xED000-0xF4000, 28 KB)
#   patched: 10 pages at 0xEA000 (0xEA000-0xF4000, 40 KB)
#
# Needed because MAX_NUM_NODES=150 makes nodes.proto up to ~30 KB worst case
# (see .notes/nodedb-3tier-sizing.md). Non-840 nRF52 parts keep the stock
# layout: their region starts at 0x6D000 and an expansion would overlap the 0x74000
# bootloader.
#
# CONSEQUENCES:
#  - FORMAT BREAK: moving the partition start relocates littlefs block 0, so
#    existing devices reformat (prefs + BLE bonds lost) on first boot.
#  - The app image must end below 0xEA000. Our in-repo linker script
#    (nrf52840_s140_v7.ld) enforces this at link time; boards on the
#    framework-default script are covered by the post-link guard below.
#  - The patch mutates the shared PlatformIO package, affecting every project
#    on this machine that builds with it. A package update/reinstall reverts
#    it; this script then just re-applies.
import os

Import("env")

FS_REGION_BASE = 0xEA000  # app flash must end below this

STOCK_BLOCK = """#ifdef NRF52840_XXAA
  #define LFS_FLASH_ADDR        0xED000
#else
  #define LFS_FLASH_ADDR        0x6D000
#endif

#define LFS_FLASH_TOTAL_SIZE  (7*FLASH_NRF52_PAGE_SIZE)"""

PATCHED_BLOCK = """// PATCHED by MeshtasticFirmware extra_scripts/nrf52_littlefs_expand.py:
// nRF52840 LittleFS expanded 28 KB -> 40 KB (10 pages at 0xEA000) for the
// 150-node nodes.proto. Non-840 parts keep the stock 7-page layout (their
// region abuts the 0x74000 bootloader).
#ifdef NRF52840_XXAA
  #define LFS_FLASH_ADDR        0xEA000
  #define LFS_FLASH_TOTAL_SIZE  (10*FLASH_NRF52_PAGE_SIZE)
#else
  #define LFS_FLASH_ADDR        0x6D000
  #define LFS_FLASH_TOTAL_SIZE  (7*FLASH_NRF52_PAGE_SIZE)
#endif"""


def _patch_framework():
    import sys

    pkg = env.PioPlatform().get_package_dir("framework-arduinoadafruitnrf52") or ""
    path = os.path.join(
        pkg, "libraries", "InternalFileSytem", "src", "InternalFileSystem.cpp"
    )
    if not os.path.isfile(path):
        sys.stderr.write(
            "nrf52_littlefs_expand: InternalFileSystem.cpp not found at %s\n" % path
        )
        env.Exit(1)

    with open(path, "r") as f:
        src = f.read()

    if PATCHED_BLOCK in src:
        print(
            "nrf52_littlefs_expand: framework already patched (LittleFS 40 KB at 0xEA000)"
        )
        return
    if STOCK_BLOCK not in src:
        sys.stderr.write(
            "\n*** nrf52_littlefs_expand: InternalFileSystem.cpp layout drifted -- patch "
            "pattern not found. ***\nThe framework package was probably updated. Re-derive "
            "STOCK_BLOCK from the new file (it must keep non-840 parts at 7 pages) or drop "
            "this script and MAX_NUM_NODES=150 together.\n\n"
        )
        env.Exit(1)

    with open(path, "w") as f:
        f.write(src.replace(STOCK_BLOCK, PATCHED_BLOCK))
    print(
        "nrf52_littlefs_expand: patched framework -- LittleFS now 10 pages at 0xEA000 (40 KB)"
    )


_patch_framework()

# --- post-link guard: image must end below the expanded partition ------------
# Same mechanism as nrf52_lto.py's ISR guard: flash end = __etext + sizeof(.data)
# (loaded at LMA __etext). Catches boards linking with the framework-default
# script, whose FLASH region still ends at 0xED000 and would happily place code
# inside 0xEA000-0xED000.
_tc = env.PioPlatform().get_package_dir("toolchain-gccarmnoneeabi") or ""
_NM = os.path.join(_tc, "bin", "arm-none-eabi-nm")
if not os.path.isfile(_NM):
    _NM = "arm-none-eabi-nm"  # fall back to PATH


def _assert_fs_region_clear(source, target, env):
    import subprocess
    import sys

    try:
        elf = env.subst("$BUILD_DIR/${PROGNAME}.elf")
        out = subprocess.check_output([_NM, elf], universal_newlines=True)
    except Exception as exc:  # tooling hiccup: warn loudly, don't wedge the build
        print(
            "nrf52_littlefs_expand: WARNING - region guard skipped (nm failed: %s)"
            % exc
        )
        return

    syms = {}
    for line in out.split("\n"):
        f = line.split()
        if len(f) >= 3 and f[-1] in ("__etext", "__data_start__", "__data_end__"):
            syms[f[-1]] = int(f[0], 16)
    if len(syms) != 3:
        print(
            "nrf52_littlefs_expand: WARNING - region guard skipped (linker symbols not found)"
        )
        return

    flash_end = syms["__etext"] + (syms["__data_end__"] - syms["__data_start__"])
    if flash_end > FS_REGION_BASE:
        sys.stderr.write(
            "\n*** nrf52_littlefs_expand: image ends at 0x%X, inside the expanded LittleFS "
            "region at 0x%X ***\nThe filesystem would erase this firmware's tail. Shrink the "
            "image, or revert the partition expansion (this script + MAX_NUM_NODES).\n\n"
            % (flash_end, FS_REGION_BASE)
        )
        from SCons.Script import Exit

        Exit(1)
    print(
        "nrf52_littlefs_expand: region guard OK -- image ends at 0x%X, %d KB clear of LittleFS"
        % (flash_end, (FS_REGION_BASE - flash_end) // 1024)
    )


env.AddPostAction("buildprog", _assert_fs_region_clear)
