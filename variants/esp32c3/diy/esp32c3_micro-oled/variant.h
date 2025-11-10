#ifndef _VARIANT_ESP32C3_Micro - OLED_
#define _VARIANT_ESP32C3_Micro -OLED_

/*----------------------------------------------------------------------------
 *        Headers
 *----------------------------------------------------------------------------*/

#ifdef __cplusplus
extern "C" {
#endif // __cplusplus

/*
ESP32C3 MICRO OLED PIN ASSIGNMENT

| Pin    | Function   | RF95 | Pin     | Function     | RF95 |
| ------ | ---------- | ---- | ------- | ------------ | ---- |
| 5v     |            |      | GPIO10  | MOSI         | MOSI |
| Gnd    |            |      | GPIO09  | BUTTON_PIN   |      |
| 3v3    |            |      | GPIO08  | LED_Pin      |      |
| GPIO20 |            |      | GPIO07  | CS           | NSS  |
| GPIO21 |            |      | GPIO06  | SCL          |      |
| GPIO02 | DIO1/IRQ   | DIO1 | GPIO05  | SDA          |      |
| GPIO01 | BUSY       | DIO0 | GPIO04  | SCK          | SCK  |
| GPIO00 | NRST       | RST  | GPIO03  | MISO         | MISO |

*/

// LED
#define LED_PIN 8      // LED
#define LED_STATE_ON 1 // State when LED is lit

// I2C (Wire) & OLED
#define WIRE_INTERFACES_COUNT (1)
#define I2C_SDA (5)
#define I2C_SCL (6)

#define HAS_SCREEN 1
#define USE_SSD1306_72_40

// GPS
#define HAS_GPS 0

#undef GPS_RX_PIN
#undef GPS_TX_PIN

// Button
#define BUTTON_PIN (9) // BOOT button

// LoRa
#define USE_LLCC68
#define USE_SX1262
#define USE_RF95
#define USE_SX1268

#define LORA_DIO0 (1) // Busy
#define LORA_RESET (0)
#define LORA_DIO1 (2)
#define LORA_RXEN RADIOLIB_NC // Not used
#define LORA_BUSY LORA_DIO0 
#define LORA_SCK (4)
#define LORA_MISO (3)
#define LORA_MOSI (10)
#define LORA_CS (7)

#define SX126X_CS LORA_CS
#define SX126X_DIO1 LORA_DIO1
#define SX126X_BUSY LORA_BUSY
#define SX126X_RESET LORA_RESET
#define SX126X_RXEN LORA_RXEN

#define SX126X_DIO3_TCXO_VOLTAGE (1.8)
#define TCXO_OPTIONAL // make it so that the firmware can try both TCXO and XTAL

#ifdef __cplusplus
}
#endif

/*----------------------------------------------------------------------------
 *        Arduino objects - C++ only
 *----------------------------------------------------------------------------*/

#endif
