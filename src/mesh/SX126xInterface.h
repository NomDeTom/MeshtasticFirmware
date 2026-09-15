#pragma once
#if RADIOLIB_EXCLUDE_SX126X != 1

#include "RadioLibInterface.h"
#include "configuration.h"

/**
 * \brief Adapter for SX126x radio family. Implements common logic for child classes.
 *
 * Everything here reaches the radio through an SX126x pointer, so it is compiled once however
 * many SX126x module types a variant enables. Only begin() has a per-chip signature, so only
 * that call stays in the template below.
 */
class SX126xInterfaceBase : public RadioLibInterface
{
  protected:
    SX126xInterfaceBase(LockingArduinoHal *hal, RADIOLIB_PIN_TYPE cs, RADIOLIB_PIN_TYPE irq, RADIOLIB_PIN_TYPE rst,
                        RADIOLIB_PIN_TYPE busy, PhysicalLayer *iface)
        : RadioLibInterface(hal, cs, irq, rst, busy, iface)
    {
    }

  public:
    /// Initialise the Driver transport hardware and software.
    /// Make sure the Driver is properly configured before calling init().
    /// \return true if initialisation succeeded.
    virtual bool init() override;

    /// Apply any radio provisioning changes
    /// Make sure the Driver is properly configured before calling init().
    /// \return true if initialisation succeeded.
    virtual bool reconfigure() override;

    /// Prepare hardware for sleep.  Call this _only_ for deep sleep, not needed for light sleep.
    virtual bool sleep() override;

    bool isIRQPending() override { return radio->getIrqFlags() != 0; }

    void resetAGC() override;

    void setTCXOVoltage(float voltage) { tcxoVoltage = voltage; }

  protected:
    float currentLimit = 140; // Higher OCP limit for SX126x PA
    float tcxoVoltage = 0.0;

    /// Set by the template subclass once `lora` is alive - never during base construction.
    SX126x *radio = nullptr;

    /// The one call whose signature is declared per chip rather than inherited from SX126x.
    virtual int16_t beginRadio(float freq, float bw, uint8_t sf, uint8_t cr, uint8_t syncWord, int8_t power,
                               uint16_t preambleLength, float tcxoVoltage, bool useRegulatorLDO) = 0;

    int16_t getCurrentRSSI() override;

    /**
     * Glue functions called from ISR land
     */
    virtual void clearRadioIsr() override;

    /**
     * Enable a particular ISR callback glue function
     */
    virtual void setRadioIsr(void (*callback)()) override;

#ifdef LORA_DIO1_SOFTWARE_POLL
    void handleSoftwareLoraIrqPoll() override;
#endif

    /** can we detect a LoRa preamble on the current channel? */
    virtual bool isChannelActive() override;

    /** are we actively receiving a packet (only called during receiving state) */
    virtual bool isActivelyReceiving() override;

    /**
     * Start waiting to receive a message
     */
    virtual void startReceive() override;

    /**
     *  We override to turn on transmitter power as needed.
     */
    virtual void configHardwareForSend() override;

    /**
     * Add SNR data to received messages
     */
    virtual void addReceiveMetadata(meshtastic_MeshPacket *mp) override;

    virtual void setStandby() override;

    uint32_t getPacketTime(uint32_t pl, bool received) override { return computePacketTime(*radio, pl, received); }

  private:
#ifdef LORA_DIO1_SOFTWARE_POLL
    bool irqPollingActive = false;
    bool pollTxMode = false;
#endif
    /** Some boards require GPIO control of tx vs rx paths */
    void setTransmitEnable(bool txon);

    /** Program all modem parameters into the chip; returns the first RadioLib error, or RADIOLIB_ERR_NONE */
    int16_t programModemParams();

    /** begin() and chip-side setup, shared by init() and by reconfigure()'s recovery of a chip that lost its state */
    bool reinitChip();

    /** setStandby()'s body, returning the standby error instead of asserting - for callers that can recover */
    int16_t trySetStandby();

    /** Recover a chip that lost its runtime state: hardware-reset via begin() and reprogram */
    bool recoverChipStateLoss() override { return reinitChip() && programModemParams() == RADIOLIB_ERR_NONE; }
};

/**
 * \brief Storage and construction for one concrete SX126x module type.
 * \tparam T RadioLib module type for SX126x: SX1262, SX1268, LLCC68, STM32WLx.
 *
 * `lora` keeps its position: RadioLibInterface needs its address while the base subobject is
 * built, and `lora` itself is built from `module`, which the base owns. Only the address is
 * taken there - `radio` is set in the constructor body, once the object is alive.
 */
template <class T> class SX126xInterface : public SX126xInterfaceBase
{
  public:
    SX126xInterface(LockingArduinoHal *hal, RADIOLIB_PIN_TYPE cs, RADIOLIB_PIN_TYPE irq, RADIOLIB_PIN_TYPE rst,
                    RADIOLIB_PIN_TYPE busy)
        : SX126xInterfaceBase(hal, cs, irq, rst, busy, &lora), lora(&module)
    {
        radio = &lora;
        LOG_DEBUG("SX126xInterface(cs=%d, irq=%d, rst=%d, busy=%d)", cs, irq, rst, busy);
    }

  protected:
    /// Specific module instance
    T lora;

    int16_t beginRadio(float freq, float bw, uint8_t sf, uint8_t cr, uint8_t syncWord, int8_t power, uint16_t preambleLength,
                       float tcxoVoltage, bool useRegulatorLDO) override
    {
        return lora.begin(freq, bw, sf, cr, syncWord, power, preambleLength, tcxoVoltage, useRegulatorLDO);
    }
};
#endif