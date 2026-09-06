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

    /// Set when the batch carries ages in seconds rather than epochs, because the sender had
    /// no trustworthy clock. The receiver dates them against its own: epoch = now - age.
    static constexpr uint32_t CTX_AGES_NOT_EPOCHS = 1u << 24;

    /// Pack the source portnum and variant tag for Options::context.
    static uint32_t makeContext(meshtastic_PortNum port, uint8_t variantTag, bool ages = false)
    {
        return ((uint32_t)port << 8) | variantTag | (ages ? CTX_AGES_NOT_EPOCHS : 0u);
    }

  protected:
    virtual ProcessMessage handleReceived(const meshtastic_MeshPacket &mp) override;

  private:
    void deliverToPhone(const meshtastic_MeshPacket &src, const meshtastic_Telemetry &t);
};

extern BiscuitModule *biscuitModule;

#endif // MESHTASTIC_BISCUIT_ENABLED
