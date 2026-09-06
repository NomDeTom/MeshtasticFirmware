#pragma once

#include "mesh/biscuit/Biscuit.h"

#if MESHTASTIC_BISCUIT_ENABLED

#include "SinglePortModule.h"
#include "mesh/generated/meshtastic/telemetry.pb.h"

/**
 * Receives BISCUIT_APP batches and hands each decoded reading to the phone as an ordinary
 * Telemetry message.
 *
 * The accumulator dispatches a batch either as a TelemetryRecordHistory on port 80 or as a
 * Biscuit payload on port 81; each is handled where it lands. Rewriting an 81 into an 80 and
 * re-dispatching would be transparent but needs callModules() re-entrancy and hides the
 * packet from the ACK and rebroadcast rules that name TELEMETRY_HISTORY_APP by number.
 */
class BiscuitModule : public SinglePortModule
{
  public:
    BiscuitModule();

    /// Time-quality byte, carried in the top eight bits of the context word. The low nibble is
    /// RTCQuality unchanged; the high nibble says how to read the value. Wire type only - the
    /// RTCQuality ladder in gps/RTC.h is untouched, so none of its comparisons change. Uptime
    /// is not a worse epoch, it is not an epoch, which is why it is a flag and not a tier.
    enum TimeQuality : uint8_t {
        TIMEQ_TIER_MASK = 0x0F,          ///< RTCQuality: None 0, Device 1, FromNet 2, NTP 3, GPS 4
        TIMEQ_UPTIME_BASED = 1u << 4,    ///< the value is an age in seconds, not an epoch
        TIMEQ_REBOOT_TAINTED = 1u << 5,  ///< stamped before a reboot; the uptime base is gone
        TIMEQ_RECEIVER_APPLIED = 1u << 6 ///< dated by the receiver, not by the origin
    };

    /// Pack the source portnum, variant tag and time-quality byte for Options::context.
    static uint32_t makeContext(meshtastic_PortNum port, uint8_t variantTag, uint8_t timeQuality = 0)
    {
        return ((uint32_t)timeQuality << 24) | ((uint32_t)port << 8) | variantTag;
    }
    static uint8_t timeQualityOf(uint32_t context) { return (uint8_t)(context >> 24); }

  protected:
    virtual ProcessMessage handleReceived(const meshtastic_MeshPacket &mp) override;

  private:
    void deliverToPhone(const meshtastic_MeshPacket &src, const meshtastic_Telemetry &t);
};

extern BiscuitModule *biscuitModule;

#endif // MESHTASTIC_BISCUIT_ENABLED
