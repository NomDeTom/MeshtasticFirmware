#include "mesh/biscuit/BiscuitModule.h"

#if MESHTASTIC_BISCUIT_ENABLED

#include "MeshService.h"
#include "gps/RTC.h"
#include "mesh/biscuit/BiscuitVariants.h"
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

    const uint8_t variantTag = variantOf(context);
    // A sender with no clock sends ages; date them against ours. Without a clock of our own we
    // cannot, so the readings go on undated rather than carrying a fabricated epoch.
    const bool agesNotEpochs = (context & CTX_UPTIME_BASED) != 0;
    const uint32_t nowEpoch = agesNotEpochs ? getValidTime(RTCQualityFromNet) : 0;
    // What the readings end up dated by: our tier plus RECEIVER_APPLIED where we applied it,
    // the sender's where we did not. That pairing says something neither half says alone.
    const uint32_t appliedQ =
        (agesNotEpochs && nowEpoch)
            ? (uint32_t)(((uint32_t)(getRTCQuality() & CTX_TIER_MASK) << CTX_TIER_SHIFT) | CTX_RECEIVER_APPLIED)
            : (context & ~(uint32_t)CTX_VARIANT_MASK);
    // Overlap repeats the oldest readings of each batch on purpose, so a receiver that saw the
    // previous packet would file them twice. The count is declared on the wire; all this needs
    // is the newest stamp already taken from each sender. Four senders is enough for the case
    // this exists for - a handful of accumulating nodes offloading to one collector - and a
    // sender that falls out of the table gets duplicates rather than losing readings.
    const uint8_t repeats = biscuit::peekRepeats(mp.decoded.payload.bytes, mp.decoded.payload.size);
    uint32_t newest = 0;
    uint8_t delivered = 0;

    uint32_t times[BISCUIT_MAX_BATCH];
    uint8_t n = 0;

    biscuit::Options opt;
    opt.fixed32IsFloat = true;
    // The quantum is the sender's, read off the wire - not ours. Deriving it from our own
    // configuration would rebuild every stamp wrong by that factor, and silently.
    opt.timeRes = quantumOf(context);

    // One stack buffer for the variant actually present, rather than a union of all of them.
    // Value-initialised rather than assigned TYPE##_init_zero, because HostMetrics ends in a
    // char array and a struct holding one is not assignable from a braced list.
#define BISCUIT_DECODE_VARIANT(TYPE, FIELD)                                                                                      \
    case meshtastic_Telemetry_##FIELD##_tag: {                                                                                   \
        TYPE m[BISCUIT_MAX_BATCH] = {};                                                                                          \
        void *slots[BISCUIT_MAX_BATCH];                                                                                          \
        for (uint8_t i = 0; i < BISCUIT_MAX_BATCH; i++)                                                                          \
            slots[i] = &m[i];                                                                                                    \
        n = biscuit::decode(&TYPE##_msg, mp.decoded.payload.bytes, mp.decoded.payload.size, slots, BISCUIT_MAX_BATCH, times,     \
                            opt);                                                                                                \
        for (uint8_t i = 0; i < n; i++) {                                                                                        \
            meshtastic_Telemetry t = meshtastic_Telemetry_init_zero;                                                             \
            t.time = agesNotEpochs ? ((times[i] < nowEpoch) ? nowEpoch - times[i] : 0) : times[i];                               \
            if (i < repeats && alreadySeen(mp.from, t.time))                                                                     \
                continue;                                                                                                        \
            newest = (t.time > newest) ? t.time : newest;                                                                        \
            delivered++;                                                                                                         \
            t.which_variant = meshtastic_Telemetry_##FIELD##_tag;                                                                \
            t.variant.FIELD = m[i];                                                                                              \
            deliverToPhone(mp, t);                                                                                               \
        }                                                                                                                        \
        break;                                                                                                                   \
    }

    switch (variantTag) {
        BISCUIT_METRICS_TYPES(BISCUIT_DECODE_VARIANT)
    default:
        LOG_WARN("Biscuit: unknown variant %u in 0x%08x", variantTag, mp.id);
        return ProcessMessage::STOP;
    }
#undef BISCUIT_DECODE_VARIANT

    noteDelivered(mp.from, newest);

    if (!n)
        LOG_WARN("Biscuit: 0x%08x decoded to nothing", mp.id);
    else
        LOG_INFO("Biscuit: %u of %u readings from 0x%08x delivered to phone (%u repeats declared), time quality 0x%04x",
                 delivered, n, mp.id, repeats, (unsigned)appliedQ);

    return ProcessMessage::STOP;
}

/// True if this sender has already given us a reading at or after `t`, which makes `t` one of
/// the deliberate repeats rather than something new.
bool BiscuitModule::alreadySeen(NodeNum from, uint32_t t) const
{
    for (const auto &s : lastSeen)
        if (s.from == from)
            return t != 0 && t <= s.newest;
    return false;
}

/// Record the newest stamp taken from this sender, evicting the oldest slot when full.
void BiscuitModule::noteDelivered(NodeNum from, uint32_t newest)
{
    if (!newest)
        return;
    for (auto &s : lastSeen)
        if (s.from == from) {
            if (newest > s.newest)
                s.newest = newest;
            return;
        }
    lastSeen[nextSeenSlot] = {from, newest};
    nextSeenSlot = (uint8_t)((nextSeenSlot + 1) % (sizeof(lastSeen) / sizeof(lastSeen[0])));
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
