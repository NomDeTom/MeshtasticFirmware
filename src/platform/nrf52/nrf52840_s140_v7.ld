/* Linker script to configure memory regions. */

SEARCH_DIR(.)
GROUP(-lgcc -lc -lnosys)

MEMORY
{
  /* App region ends at 0xE5000, not 0xED000: the top 8 flash pages (32 KB,
   * reclaimed by whole-image LTO) hold the warm node store's two raw-flash
   * copies (see src/mesh/WarmNodeStore.h, WARM_FLASH_REGION_BASE). LittleFS
   * still occupies 0xED000-0xF4000. Boards using the framework-default
   * linker script are covered by extra_scripts/nrf52_warm_region.py instead. */
  FLASH (rx)     : ORIGIN = 0x27000, LENGTH = 0xE5000 - 0x27000

  /* SRAM required by Softdevice depend on
   * - Attribute Table Size (Number of Services and Characteristics)
   * - Vendor UUID count
   * - Max ATT MTU
   * - Concurrent connection peripheral + central + secure links
   * - Event Len, HVN queue, Write CMD queue
   */ 
  RAM (rwx) :  ORIGIN = 0x20006000, LENGTH = 0x20040000 - 0x20006000
}

SECTIONS
{
  . = ALIGN(4);
  .svc_data :
  {
    PROVIDE(__start_svc_data = .);
    KEEP(*(.svc_data))
    PROVIDE(__stop_svc_data = .);
  } > RAM
  
  .fs_data :
  {
    PROVIDE(__start_fs_data = .);
    KEEP(*(.fs_data))
    PROVIDE(__stop_fs_data = .);
  } > RAM
} INSERT AFTER .data;

INCLUDE "nrf52_common.ld"
