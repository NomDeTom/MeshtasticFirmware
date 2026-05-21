#pragma once

// =============================================================================
//   Left:  4,5,6,7 | 15,16,17,18 | 8,3,46,9,10,11,12,13,14
//   Right: 43(TX),44(RX),1,2,42,41,40,39,38,37,36,35 | 0,45,48,47,21,20,19
//   PSRAM (unavailable): 35,36,37
//   Strapping (use with care): 0(Boot), 3(JTAG), 45(VSPI), 46(LOG)
//   USB (avoid): 19(D+), 20(D-)
//   Onboard LEDs: 2(ON), 43(TX), 44(RX), 48(WS2812)
// =============================================================================

#define I2C_SDA                     21
#define I2C_SCL                     20


#define BUTTON_PIN                  0       // BOOT button


#define HAS_SCREEN                  1



#define ST7789_CS                   10
#define ST7789_SCK                  12      // SPI clock
#define ST7789_SDA                  11      // SPI MOSI (TFT data in)
#define ST7789_RS                   13      // Data/Command
#define ST7789_RESET                14      // Hardware reset
#define ST7789_BL                   9       // Backlight PWM

#define TFT_BL                      ST7789_BL

#define USE_TFTDISPLAY              1

#define ST7789_MISO                 -1      // Not connected 
#define ST7789_BUSY                 -1      // Not connected

#define TFT_BACKLIGHT_ON            HIGH

#define TFT_WIDTH                   320
#define TFT_HEIGHT                  172

#define TFT_OFFSET_X                0
#define TFT_OFFSET_Y                0
#define TFT_OFFSET_ROTATION         0



#define SPI_FREQUENCY          40000000
#define SPI_READ_FREQUENCY     10000000
#define SCREEN_ROTATE               0


#define HAS_GPS                     0

#define USE_SX1262
#define SX126X_CS                15
#define SX126X_DIO1              16
#define SX126X_BUSY              17
#define SX126X_RESET             18
#define SX126X_RXEN              8
#define SX126X_TXEN              7

//
// #define HAS_GPS                  1
// #define GPS_UBLOX
// #define GPS_RX_PIN               5
// #define GPS_TX_PIN               6
