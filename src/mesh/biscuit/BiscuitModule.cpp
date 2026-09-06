#include "mesh/biscuit/BiscuitModule.h"

#if MESHTASTIC_BISCUIT_ENABLED

#include "MeshService.h"
#include "gps/RTC.h"
#include "mesh/generated/meshtastic/telemetry.pb.h"

BiscuitModule *biscuitModule;

BiscuitModule::BiscuitModule() : SinglePortModule("biscuit", meshtastic_PortNum_BISCUIT_APP) {}

/**
 * Decode a batch and hand each reading to the phone as an ordinary Telemetry message.
 *
 * Deliberately not transparent: the packet is not rewritten and re-dispatched as a
 * TelemetryRecordHistory. Doing that would mean calling callModules() from inside
 * callModules(), which Router.cpp already has to defend against, and it would leave Biscuit
 * packets invisible to the ACK and rebroadcast rules that name TELEMETRY_HISTORY_APP.
 * A batch arrives on 81 or on 80; each port is handled where it lands.
 */
ProcessMessage BiscuitModule::handleReceived(const meshtastic_MeshPacket &mp)
{
    uint32_t context = 0;
    if (!biscuit::peekContext(mp.decoded.payload.bytes, mp.decoded.payload.size, &context)) {
        LOG_WARN("Biscuit: 0x%08x is not a Biscuit payload", mp.id);
        return ProcessMessage::STOP;
    }

    const uint8_t variantTag = context & 0xFF;
    // A sender with no clock sends ages; date them against ours. Without a clock of our own we
    // cannot, so the readings go on undated rather than carrying a fabricated epoch.
    const bool agesNotEpochs = (context & CTX_AGES_NOT_EPOCHS) != 0;
    const uint32_t nowEpoch = agesNotEpochs ? getValidTime(RTCQualityFromNet) : 0;
    uint32_t times[BISCUIT_MAX_BATCH];
    uint8_t n = 0;

    biscuit::Options opt;
    opt.fixed32IsFloat = true;

    // One stack buffer for the variant actually present, rather than a union of all of them.
#define BISCUIT_DECODE_VARIANT(TAG, TYPE, FIELD)                                                                                 \
    case TAG: {                                                                                                                  \
        TYPE m[BISCUIT_MAX_BATCH];                                                                                               \
        void *slots[BISCUIT_MAX_BATCH];                                                                                          \
        for (uint8_t i = 0; i < BISCUIT_MAX_BATCH; i++) {                                                                        \
            m[i] = TYPE##_init_zero;                                                                                             \
            slots[i] = &m[i];                                                                                                    \
        }                                                                                                                        \
        n = biscuit::decode(&TYPE##_msg, mp.decoded.payload.bytes, mp.decoded.payload.size, slots, BISCUIT_MAX_BATCH, times,     \
                            opt);                                                                                                \
        for (uint8_t i = 0; i < n; i++) {                                                                                        \
            meshtastic_Telemetry t = meshtastic_Telemetry_init_zero;                                                             \
            t.time = agesNotEpochs ? ((times[i] < nowEpoch) ? nowEpoch - times[i] : 0) : times[i];                               \
            t.which_variant = meshtastic_Telemetry_##FIELD##_tag;                                                                \
            t.variant.FIELD = m[i];                                                                                              \
            deliverToPhone(mp, t);                                                                                               \
        }                                                                                                                        \
        break;                                                                                                                   \
    }

    switch (variantTag) {
        BISCUIT_DECODE_VARIANT(meshtastic_TelemetryRecord_environment_metrics_tag, meshtastic_EnvironmentMetrics,
                               environment_metrics)
        BISCUIT_DECODE_VARIANT(meshtastic_TelemetryRecord_power_metrics_tag, meshtastic_PowerMetrics, power_metrics)
        BISCUIT_DECODE_VARIANT(meshtastic_TelemetryRecord_air_quality_metrics_tag, meshtastic_AirQualityMetrics,
                               air_quality_metrics)
    default:
        LOG_WARN("Biscuit: unknown variant %u in 0x%08x", variantTag, mp.id);
        return ProcessMessage::STOP;
    }
#undef BISCUIT_DECODE_VARIANT

    if (!n)
        LOG_WARN("Biscuit: 0x%08x decoded to nothing", mp.id);
    else
        LOG_INFO("Biscuit: %u readings from 0x%08x delivered to phone", n, mp.id);

    return ProcessMessage::STOP;
}

/// One decoded reading, sent on as if the sender had published it individually.
void BiscuitModule::deliverToPhone(const meshtastic_MeshPacket &src, const meshtastic_Telemetry &t)
{
    meshtastic_MeshPacket *p = allocDataPacket();
    if (!p)
        return;
    p->decoded.portnum = meshtastic_PortNum_TELEMETRY_APP;
    p->from = src.from;
    p->to = src.to;
    p->channel = src.channel;
    p->rx_time = t.time;
    p->decoded.payload.size =
        pb_encode_to_bytes(p->decoded.payload.bytes, sizeof(p->decoded.payload.bytes), &meshtastic_Telemetry_msg, &t);
    if (p->decoded.payload.size)
        service->sendToPhone(p);
    else
        packetPool.release(p);
}

#endif // MESHTASTIC_BISCUIT_ENABLED
