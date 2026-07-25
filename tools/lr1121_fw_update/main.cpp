/*
  LR1121 on-chip firmware updater for the nrf52_promicro_diy_tcxo board.

  Standalone utility (NOT part of the Meshtastic application) - adapted from RadioLib's
  own example:
    https://github.com/jgromes/RadioLib/blob/master/examples/LR11x0/LR11x0_Firmware_Update/LR11x0_Firmware_Update.ino

  Why this exists: a bare (non-TCXO) LR1121 module tested on this board was found running
  base firmware v1.1 (0x0101) - three revisions behind the latest available (0x0104) - and
  hangs during radio bring-up (see .notes/hardware/lr1121-tcxo-hang/README.md for the full
  writeup). This sketch updates that chip's on-die firmware in isolation, outside the full
  Meshtastic build, so the hang can be re-tested against current firmware.

  Pins match this board's actual wiring (variants/nrf52840/diy/nrf52_promicro_diy_tcxo/variant.h):
    LORA_CS    = 45  (P1.13)
    LORA_DIO1  = 10  (P0.10, IRQ)
    LORA_RESET = 9   (P0.09, NRST)
    LORA_DIO0  = 29  (P0.29, BUSY)

  Build/upload (from the repo root):
    pio run -e lr1121_fw_update -t upload
    pio device monitor -e lr1121_fw_update

  USE WITH CARE: firmware updates can brick the radio if interrupted or given the wrong
  image. This sketch prompts for confirmation before writing anything.
*/

#include <RadioLib.h>

// Select the firmware image to upload - latest available per RadioLib as of this writing.
// Change this to target a different revision if needed.
#define RADIOLIB_LR1121_FIRMWARE_0104

// Firmware images are large (up to ~240 kB) - keep them in flash, not RAM (RAM here is only
// 248832 bytes total, no room to spare). Do NOT define RADIOLIB_LR1110_FIRMWARE_IN_RAM.
#include <modules/LR11x0/LR11x0_firmware.h>

// This board's actual radio pins: cs, irq, rst, busy
LR1121 radio = new Module(45, 10, 9, 29);

// This tool exists specifically for the bare (non-TCXO) LR1121 found during bring-up, so try
// XTAL mode FIRST - trying TCXO first would hit the exact calibration-wait hang this whole
// investigation was chasing (see .notes/hardware/lr1121-tcxo-hang/), before this sketch's own
// fallback ever gets a chance to run. TCXO is attempted second in case a TCXO-equipped module
// is ever tested with this same tool; the vendored RadioLib copy for this env has also been
// patched with a bounded calibration-wait timeout, so that attempt can no longer hang either.
static const float TCXO_VOLTAGE = 1.8;

bool radioBegin()
{
    ConfigLoRa_t config;
    config.frequency = 434;

    Serial.print(F("[LR1121] Initializing without TCXO (XTAL mode) ... "));
    radio.tcxoVoltage = 0;
    int state = radio.begin(config);
    if (state == RADIOLIB_ERR_NONE) {
        Serial.println(F("success!"));
        return true;
    }
    Serial.print(F("failed, code "));
    Serial.println(state);

    Serial.print(F("[LR1121] Retrying with TCXO ("));
    Serial.print(TCXO_VOLTAGE);
    Serial.print(F("V) ... "));
    radio.tcxoVoltage = TCXO_VOLTAGE;
    state = radio.begin(config);
    if (state == RADIOLIB_ERR_NONE) {
        Serial.println(F("success!"));
        return true;
    }
    Serial.print(F("failed, code "));
    Serial.println(state);
    return false;
}

void printVersions()
{
    LR11x0VersionInfo_t version;
    Serial.print(F("[LR1121] Reading firmware versions ... "));
    int16_t state = radio.getVersionInfo(&version);
    if (state != RADIOLIB_ERR_NONE) {
        Serial.print(F("failed, code "));
        Serial.println(state);
        return;
    }
    Serial.println(F("success!"));

    Serial.print(F("[LR1121] Device: "));
    Serial.println(version.device);

    Serial.print(F("[LR1121] Base firmware: "));
    Serial.print(version.fwMajor);
    Serial.print('.');
    Serial.println(version.fwMinor);
}

void setup()
{
    Serial.begin(115200);

    // Don't block forever waiting for a terminal to attach - TinyUSB's Serial boolean can stay
    // false indefinitely depending on the host/terminal (unlike a native USB CDC ACM), which
    // would otherwise hang here with zero output ever appearing if the monitor wasn't already
    // attached before/during this wait. Bound it, then proceed regardless.
    uint32_t waitStart = millis();
    while (!Serial && (millis() - waitStart < 5000))
        delay(10);
    delay(500); // give the host a moment to finish attaching right after enumeration

    // TinyUSB CDC has no real backlog buffer, so anything printed before a terminal actually
    // starts reading can simply be lost - repeat this banner for a few seconds so a monitor
    // attached a little late still catches it, instead of everything after this point looking
    // like silence.
    for (uint8_t i = 0; i < 10; i++) {
        Serial.println(F("[LR1121] fw update tool ready ..."));
        delay(300);
    }

    if (!radioBegin()) {
        Serial.println(F("[LR1121] Could not initialize radio at all - check wiring. Halting."));
        while (true)
            delay(10);
    }

    printVersions();

    // Gate the update on the board's physical BUTTON_PIN instead of a serial keystroke - a
    // non-interactive serial monitor can't send input, but this needs *some* deliberate
    // confirmation so the firmware doesn't get rewritten on every power cycle. Standard
    // active-low/pull-up wiring (button to GND) - flip the LOW check below if this board's
    // button is wired the other way.
    pinMode(BUTTON_PIN, INPUT_PULLUP);
    Serial.println();
    Serial.println(F("[LR1121] This will overwrite the on-chip firmware. Do not disconnect power"));
    Serial.println(F("[LR1121] or reset the board once started."));
    Serial.println(F("[LR1121] Press and hold the BUTTON now to begin - waiting 10s ..."));
    bool confirmed = false;
    uint32_t confirmStart = millis();
    while (millis() - confirmStart < 10000) {
        if (digitalRead(BUTTON_PIN) == LOW) {
            delay(50); // debounce
            if (digitalRead(BUTTON_PIN) == LOW) {
                confirmed = true;
                break;
            }
        }
        delay(10);
    }
    if (!confirmed) {
        Serial.println(F("[LR1121] No button press seen - skipping update. Reset to try again."));
        while (true)
            delay(10);
    }
    Serial.println(F("[LR1121] Confirmed."));

    Serial.print(F("[LR1121] Updating firmware, this may take several seconds ... "));
    int state = radio.updateFirmware(lr11xx_firmware_image, RADIOLIB_LR11X0_FIRMWARE_IMAGE_SIZE);
    if (state == RADIOLIB_ERR_NONE) {
        Serial.println(F("success!"));
    } else {
        Serial.print(F("failed, code "));
        Serial.println(state);
    }

    printVersions();
}

void loop() {}
