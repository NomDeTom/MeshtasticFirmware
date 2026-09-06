#include "BaseTelemetryModule.h"
#include "PowerTelemetry.h"
#include "UptimeClock.h"
#include "gps/RTC.h"
#include "mesh/biscuit/Biscuit.h"
#include "mesh/biscuit/BiscuitModule.h"

#if HAS_TELEMETRY && !MESHTASTIC_EXCLUDE_AIR_QUALITY_SENSOR
#include "AirQualityTelemetry.h"
#endif

/**
 * Build a packet from an already-populated reading and send it to the mesh or the phone.
 * @return true if the packet was built and handed off to send
 */
bool BaseTelemetryModule::publishTelemetry(meshtastic_Telemetry &m, NodeNum dest, bool phoneOnly)
{
    meshtastic_MeshPacket *p = allocTelemetryPacket(m);
    if (!p)
        return false;

    p->to = dest;
    p->decoded.want_response = false;
    p->priority = config.device.role == meshtastic_Config_DeviceConfig_Role_SENSOR ? meshtastic_MeshPacket_Priority_RELIABLE
                                                                                   : meshtastic_MeshPacket_Priority_BACKGROUND;

    onPublishedTelemetry(*p);

    if (phoneOnly) {
        LOG_INFO("Sending packet to phone");
        service->sendToPhone(p);
    } else {
        LOG_INFO("Sending packet to mesh");
        service->sendToMesh(p, RX_SRC_LOCAL, true);
    }
    return true;
}

// Mesh is airtime-constrained, so its packets stay well under the wire array's full capacity
// regardless of how big that capacity is. Mqtt has no equivalent per-packet cost, so it can use
// the array's full capacity - the actual per-packet ceiling remains the 233-byte
// meshtastic_Data.payload limit, still enforced dynamically below by the shed-and-retry loop
// regardless of this cap; this only raises how many we're willing to *attempt* fitting.
constexpr size_t kMaxReadingsPerMeshPacket = MESHTASTIC_MAX_READINGS_PER_MESH_PACKET;
constexpr size_t kMaxReadingsPerMqttPacket =
    sizeof(meshtastic_TelemetryRecordHistory::readings) / sizeof(meshtastic_TelemetryRecordHistory::readings[0]);
static_assert(kMaxReadingsPerMeshPacket <= kMaxReadingsPerMqttPacket, "mesh's cap is meant to be the smaller of the two");

/**
 * Encode up to maxTake readings (indices, oldest first) into p's payload, shedding the newest of
 * the batch and retrying until it fits the 233-byte meshtastic_Data.payload limit.
 *
 * Deliberately its own non-inlined function to avoid stack problems.
 */
template <typename T, uint8_t N>
static __attribute__((noinline)) size_t encodeHistoryBatch(meshtastic_MeshPacket &p, const TelemetryHistoryBuffer<T, N> &history,
                                                           const uint8_t *indices, size_t maxTake)
{
    size_t take = maxTake;
    while (take > 0) {
        meshtastic_TelemetryRecordHistory recordHistory = meshtastic_TelemetryRecordHistory_init_zero;

        recordHistory.readings_count = take;

        for (size_t i = 0; i < take; i++) {
            assignTelemetryRecord(recordHistory.readings[i], history.at(indices[i]));
        }

        p.decoded.payload.size = pb_encode_to_bytes(p.decoded.payload.bytes, sizeof(p.decoded.payload.bytes),
                                                    &meshtastic_TelemetryRecordHistory_msg, &recordHistory);
        if (p.decoded.payload.size > 0)
            return take;
        take--;
    }
    return 0;
}

#if MESHTASTIC_BISCUIT_DIVERT
/**
 * Re-encode the same readings columnwise. Returns how many fitted, 0 if Biscuit declined -
 * which it does when the result would be no smaller than the protobuf it replaces, so a
 * refusal is a correct outcome and the caller falls back to encodeHistoryBatch().
 *
 * Shrinks the batch on overflow exactly as encodeHistoryBatch does, so a node never fails to
 * publish merely because the newest readings would not fit.
 */
template <typename T, uint8_t N>
static __attribute__((noinline)) size_t encodeBiscuitBatch(meshtastic_MeshPacket &p, const TelemetryHistoryBuffer<T, N> &history,
                                                           const uint8_t *indices, size_t maxTake)
{
    if (maxTake > BISCUIT_MAX_BATCH)
        maxTake = BISCUIT_MAX_BATCH;

    // Date the batch. A node with a trustworthy clock converts any reading captured before the
    // clock arrived, exactly as MeshService::reconcilePendingRxTimes() does for the phone queue -
    // both stamps are monotonic uptime, so the elapsed term is exact at any age. A node that has
    // never had a clock sends ages instead, and the receiver dates them against its own.
    const uint32_t nowEpoch = getValidTime(RTCQualityFromNet);
    const uint32_t nowUptime = Time::getUptimeSecs();
    const bool sendAges = (nowEpoch == 0);

    biscuit::Options opt;
    opt.fixed32IsFloat = true; // every fixed32 in a telemetry message is a float
    opt.context = BiscuitModule::makeContext(meshtastic_PortNum_TELEMETRY_HISTORY_APP, variantTagFor<T>(), sendAges);

    const void *msgs[BISCUIT_MAX_BATCH];
    uint32_t times[BISCUIT_MAX_BATCH];
    for (size_t take = maxTake; take > 0; take--) {
        for (size_t i = 0; i < take; i++) {
            const BufferedReading<T> &b = history.at(indices[i]);
            msgs[i] = &b.metrics;
            const uint32_t age = nowUptime - b.uptimeSecs; // monotonic, exact across the wrap
            if (sendAges)
                times[i] = age;
            else if (b.time)
                times[i] = b.time;
            else
                times[i] = (age < nowEpoch) ? nowEpoch - age : 0; // back-date, or leave undated
        }
        biscuit::Result r = biscuit::encode(metricsDescriptor<T>(), msgs, (uint8_t)take, times, p.decoded.payload.bytes,
                                            sizeof(p.decoded.payload.bytes), opt);
        if (r.size) {
            p.decoded.payload.size = r.size;
            LOG_DEBUG("Biscuit tier %u: %u readings in %u B (protobuf would be %u B)", r.tier, (unsigned)take, (unsigned)r.size,
                      (unsigned)r.baseline);
            return take;
        }
    }
    return 0;
}
#endif // MESHTASTIC_BISCUIT_DIVERT

/**
 * Publish every reading in history not yet marked for target's channel as a single
 * TelemetryRecordHistory
 *
 * @return true if a packet was sent
 */
template <typename T, uint8_t N>
bool BaseTelemetryModule::publishBufferedTelemetry(TelemetryHistoryBuffer<T, N> &history, PublishTarget target)
{
    if (history.isEmpty())
        return false;

    TelemetryPublishChannel channelBit = target == PublishTarget::Mesh ? TELEMETRY_PUBLISHED_MESH : TELEMETRY_PUBLISHED_MQTT;

    // Oldest-first indices not yet delivered to this channel; readings already sent on a
    // previous call (or on the other channel's buffer, which has its own mask) are skipped.
    uint8_t indices[N];
    uint8_t unpublishedCount = 0;

    for (uint8_t i = 0; i < history.size(); i++) {
        if (!(history.at(i).publishedMask & channelBit))
            indices[unpublishedCount++] = i;
    }

    if (unpublishedCount == 0)
        return false;

    // Hold the batch until enough readings accumulate. Below ~6 the column framing costs more
    // than the tags it removes, so flushing early spends airtime to save nothing.
    if (MESHTASTIC_BISCUIT_FLUSH_COUNT && unpublishedCount < MESHTASTIC_BISCUIT_FLUSH_COUNT &&
        unpublishedCount < history.size()) {
        LOG_DEBUG("Holding %u/%u buffered readings until %u", (unsigned)unpublishedCount, (unsigned)history.size(),
                  (unsigned)MESHTASTIC_BISCUIT_FLUSH_COUNT);
        return false;
    }

    meshtastic_MeshPacket *p = allocTelemetryHistoryPacket();
    if (!p)
        return false;

    p->decoded.portnum = meshtastic_PortNum_TELEMETRY_HISTORY_APP;
    p->decoded.want_response = false;
    p->priority = meshtastic_MeshPacket_Priority_RELIABLE;

    const size_t maxReadingsPerPacket = target == PublishTarget::Mesh ? kMaxReadingsPerMeshPacket : kMaxReadingsPerMqttPacket;
    const size_t want = min((size_t)unpublishedCount, maxReadingsPerPacket);

    size_t take = 0;
#if MESHTASTIC_BISCUIT_DIVERT
    // Biscuit first; it declines rather than emit a packet larger than the protobuf, and a
    // decline leaves the payload untouched for the fallback below.
    take = encodeBiscuitBatch(*p, history, indices, want);
    if (take)
        p->decoded.portnum = meshtastic_PortNum_BISCUIT_APP;
#endif
    if (!take)
        take = encodeHistoryBatch(*p, history, indices, want);

    if (take == 0) {
        packetPool.release(p);
        return false;
    }

    // Attempt delivery
    bool sent = false;
    if (target == PublishTarget::Mesh) {
        // TODO(mqtt-dedup): if this node also has PublishTarget::Mqtt active (moduleConfig.
        // mqtt.telemetry_uplink_enabled + mqtt->isConnectedDirectly()) AND the channel this
        // goes out on has uplink_enabled, the Router's normal channel-uplink hook
        // (MQTT::onSend) will ALSO publish this same batch to MQTT - so these same readings
        // are still marked "unpublished for Mqtt" and get sent AGAIN, on the next
        // direct-publish cycle.

        // fire-and-forget enqueue
        service->sendToMesh(p, RX_SRC_LOCAL, false);
        sent = true;
    } else if (target == PublishTarget::Mqtt) {
        // Mqtt target: publish straight to broker connection
        sent = mqtt && mqtt->publishOwnPacket(*p);
        packetPool.release(p);
    } else {
        // Phone (or anything else) isn't a supported target for buffered history
        LOG_WARN("publishBufferedTelemetry: unsupported PublishTarget, dropping");
        packetPool.release(p);
        return false;
    }

    if (!sent)
        return false;

    // Retire everything except the overlap tail, which rides again in the next packet so a
    // lost one does not take its readings with it. Always retire at least one, or a node that
    // sets overlap >= take would resend the same batch forever.
    const size_t retire = biscuit::retireCount(take, MESHTASTIC_BISCUIT_OVERLAP_COUNT);
    for (size_t i = 0; i < retire; i++)
        history.markPublished(indices[i], channelBit);

    LOG_INFO("Publishing %u/%u buffered telemetry readings to %s (%u retired, %u repeat next time, %u still pending)",
             (unsigned)take, (unsigned)unpublishedCount, target == PublishTarget::Mesh ? "mesh" : "mqtt", (unsigned)retire,
             (unsigned)(take - retire), (unsigned)(unpublishedCount - take));

    return true;
}

// Register each concrete (metrics type, buffer size) combination that actually uses
// publishBufferedTelemetry.
#if HAS_TELEMETRY && !MESHTASTIC_EXCLUDE_AIR_QUALITY_SENSOR
template bool BaseTelemetryModule::publishBufferedTelemetry<meshtastic_AirQualityMetrics, AIR_QUALITY_TELEMETRY_HISTORY_SIZE>(
    TelemetryHistoryBuffer<meshtastic_AirQualityMetrics, AIR_QUALITY_TELEMETRY_HISTORY_SIZE> &, PublishTarget);
#endif
#if HAS_TELEMETRY && !MESHTASTIC_EXCLUDE_POWER_TELEMETRY
template bool BaseTelemetryModule::publishBufferedTelemetry<meshtastic_PowerMetrics, POWER_TELEMETRY_HISTORY_SIZE>(
    TelemetryHistoryBuffer<meshtastic_PowerMetrics, POWER_TELEMETRY_HISTORY_SIZE> &, PublishTarget);
#endif
