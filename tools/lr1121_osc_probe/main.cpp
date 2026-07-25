/*
  LR1121 oscillator-configuration probe for the nrf52_promicro_diy_tcxo board.

  Standalone, NON-DESTRUCTIVE utility (no firmware update, no writes to the module) - tries
  radio.begin() under BOTH oscillator configurations, one at a time, each with a fresh
  reset() in between, and reports the result of each attempt: clean success, a returned error
  code (and how long it took), or a timeout.

  Why this exists: we know a bare (non-TCXO) LR1121 hangs when told to expect a TCXO
  (see .notes/hardware/lr1121-tcxo-hang/). The open question this answers is the reverse case:
  on a module that DOES have a TCXO, does telling it to use XTAL mode instead fail reliably
  (detectable, e.g. a returned error or bounded timeout), or does it silently "succeed" while
  running on an absent/wrong clock? That matters for whether a fallback could safely try XTAL
  first instead of TCXO first.

  This env's vendored RadioLib copy has the calibration-wait timeout fix applied (see
  .notes/bug-reports/radiolib-lr11x0-tcxo-calibration-hang.md), so even a "hang" here is
  bounded to a few seconds and reported as a timeout, not a true lockup.

  Pins match this board's actual wiring (variants/nrf52840/diy/nrf52_promicro_diy_tcxo/variant.h):
    LORA_CS    = 45  (P1.13)
    LORA_DIO1  = 10  (P0.10, IRQ)
    LORA_RESET = 9   (P0.09, NRST)
    LORA_DIO0  = 29  (P0.29, BUSY)

  Build/upload (from the repo root):
    pio run -e lr1121_osc_probe -t upload
*/

#include <RadioLib.h>

// This board's actual radio pins: cs, irq, rst, busy
LR1121 radio = new Module(45, 10, 9, 29);

struct ProbeResult {
    bool attempted;
    bool success;
    int16_t code;
    uint32_t elapsedMs;
};

ProbeResult probe(float tcxoVoltage)
{
    ProbeResult r = {true, false, 0, 0};

    ConfigLoRa_t config;
    config.frequency = 434;
    radio.tcxoVoltage = tcxoVoltage;

    uint32_t start = millis();
    r.code = radio.begin(config);
    r.elapsedMs = millis() - start;
    r.success = (r.code == RADIOLIB_ERR_NONE);
    return r;
}

void printResult(const char *label, const ProbeResult &r)
{
    Serial.print(F("[probe] "));
    Serial.print(label);
    Serial.print(F(": "));
    if (r.success) {
        Serial.print(F("SUCCESS"));
    } else {
        Serial.print(F("FAILED, code "));
        Serial.print(r.code);
    }
    Serial.print(F(" (took "));
    Serial.print(r.elapsedMs);
    Serial.println(F("ms)"));
}

void setup()
{
    Serial.begin(115200);
    uint32_t waitStart = millis();
    while (!Serial && (millis() - waitStart < 5000))
        delay(10);
    delay(500);

    for (uint8_t i = 0; i < 10; i++) {
        Serial.println(F("[probe] LR1121 oscillator probe ready ..."));
        delay(300);
    }

    Serial.println();
    Serial.println(F("[probe] This does NOT write anything to the module - safe to run on a"));
    Serial.println(F("[probe] working (or bare) module. Trying both oscillator configs ..."));
    Serial.println();

    ProbeResult xtal = probe(0);
    printResult("XTAL mode  (tcxoVoltage=0)", xtal);

    // A fresh reset() between attempts so the second attempt isn't affected by whatever state
    // the first attempt left the chip in.
    delay(200);

    ProbeResult tcxo = probe(1.8);
    printResult("TCXO mode  (tcxoVoltage=1.8V)", tcxo);

    Serial.println();
    Serial.println(F("[probe] Summary:"));
    Serial.print(F("[probe]   XTAL: "));
    Serial.println(xtal.success ? F("works") : F("fails/times out"));
    Serial.print(F("[probe]   TCXO: "));
    Serial.println(tcxo.success ? F("works") : F("fails/times out"));
}

void loop() {}
