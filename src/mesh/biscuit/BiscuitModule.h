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

    /**
     * Context word layout. The time basis, its quality and its quantum sit in one contiguous
     * run so a receiver reads them together, and the variant sits at the bottom where a varint
     * is cheapest - the word is varint-encoded, so magnitude is cost.
     *
     *   bits 0-2    variant, the Telemetry oneof tag less 2 (eight members)
     *   bits 3-6    RTC tier, the RTCQuality values unchanged
     *   bit  7      UPTIME_BASED      the time column holds ages in seconds, not epochs
     *   bit  8      REBOOT_TAINTED    stamped before a reboot; the uptime base is gone
     *   bit  9      RECEIVER_APPLIED  dated by the receiver, not by the origin
     *   bits 10-13  quantum, an index into kTimeQuanta
     *
     * Wire type only: RTCQuality in gps/RTC.h is untouched, so none of its comparisons change.
     * Uptime is a flag and not a tier because it is not a worse epoch, it is not an epoch;
     * placing it above RTCQualityGPS would make it read as better than GPS everywhere.
     *
     * The portnum this word used to carry is gone. Nothing read it, it was a constant, and
     * sitting between the two fields that are used it pushed the quality byte to bit 24 and
     * cost three varint bytes a packet.
     */
    enum Ctx : uint32_t {
        CTX_VARIANT_MASK = 0x07,
        CTX_TIER_SHIFT = 3,
        CTX_TIER_MASK = 0x0F,
        CTX_UPTIME_BASED = 1u << 7,
        CTX_REBOOT_TAINTED = 1u << 8,
        CTX_RECEIVER_APPLIED = 1u << 9,
        CTX_QUANTUM_SHIFT = 10,
        CTX_QUANTUM_MASK = 0x0F,
    };

    /// Timestamp quanta a batch can declare. Index 0 is 1 second - exact - which is the
    /// default, so the field costs nothing until a user asks for a coarser stamp.
    static constexpr uint16_t kTimeQuanta[16] = {1, 2, 5, 10, 15, 20, 30, 60, 120, 180, 300, 600, 900, 1800, 3600, 7200};

    /// Nearest quantum not exceeding `secs`, so a coarsening request never silently becomes
    /// finer than asked for. Anything below 2 s is exact.
    static uint8_t quantumCode(uint16_t secs)
    {
        uint8_t code = 0;
        for (uint8_t i = 1; i < 16; i++)
            if (kTimeQuanta[i] <= secs)
                code = i;
        return code;
    }

    static uint32_t makeContext(uint8_t variantTag, uint8_t rtcTier, uint32_t flags, uint16_t timeRes)
    {
        return (uint32_t)((variantTag - 2) & CTX_VARIANT_MASK) | ((uint32_t)(rtcTier & CTX_TIER_MASK) << CTX_TIER_SHIFT) | flags |
               ((uint32_t)quantumCode(timeRes) << CTX_QUANTUM_SHIFT);
    }

    static uint8_t variantOf(uint32_t context) { return (uint8_t)((context & CTX_VARIANT_MASK) + 2); }
    static uint8_t rtcTierOf(uint32_t context) { return (uint8_t)((context >> CTX_TIER_SHIFT) & CTX_TIER_MASK); }
    static uint16_t quantumOf(uint32_t context) { return kTimeQuanta[(context >> CTX_QUANTUM_SHIFT) & CTX_QUANTUM_MASK]; }

  protected:
    virtual ProcessMessage handleReceived(const meshtastic_MeshPacket &mp) override;

  private:
    void deliverToPhone(const meshtastic_MeshPacket &src, const meshtastic_Telemetry &t);
};

extern BiscuitModule *biscuitModule;

#endif // MESHTASTIC_BISCUIT_ENABLED
