// ---- System I2C bus (Kode Dot): SDA=48, SCL=47
#define I2C_SDA 48
#define I2C_SCL 47

// ---- Battery Declaration Below

// --- Battery Charger PPM BQ25896
#define HAS_PPM 1
#define XPOWERS_CHIP_BQ25896

// --- Battery Quality Management BQ27220
#define HAS_BQ27220 1
#define BQ27220_I2C_SDA I2C_SDA
#define BQ27220_I2C_SCL I2C_SCL
#define BQ27220_DESIGN_CAPACITY 500

// ---- Neopixel (addressable LED)
#define HAS_NEOPIXEL 1
#define NEOPIXEL_COUNT 1
#define NEOPIXEL_DATA 4
#define NEOPIXEL_TYPE (NEO_GRB + NEO_KHZ800)
#define ENABLE_AMBIENTLIGHTING

// ---- LoRa (LR1121 on Radio module)
#define USE_LR1121
#define LORA_SCK   39
#define LORA_MISO  41
#define LORA_MOSI  40
#define LORA_CS    3
#define LORA_RESET 2

#ifdef USE_LR1121
  #define LR1121_IRQ_PIN       12
  #define LR1121_NRESET_PIN    LORA_RESET
  #define LR1121_BUSY_PIN      13
  #define LR1121_SPI_NSS_PIN   LORA_CS
  #define LR1121_SPI_SCK_PIN   LORA_SCK
  #define LR1121_SPI_MOSI_PIN  LORA_MOSI
  #define LR1121_SPI_MISO_PIN  LORA_MISO
  #define LR11X0_DIO3_TCXO_VOLTAGE 1.8
  #define LR11X0_DIO_AS_RF_SWITCH
#endif

#define HAS_GPS 1

#define GPS_TX_PIN 43
#define GPS_RX_PIN 44
