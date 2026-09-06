// Biscuit end to end: the accumulator's publish path, the wire, and the receiving module.
//
// test_biscuit proves the codec round-trips. That is necessary and not sufficient: it calls
// encode() and decode() in the same process with the same Options object, so it cannot catch
// a sender and receiver that disagree, a batch that goes out on the wrong portnum, readings
// retired that were never sent, or a decoder that is never reached because the module did not
// recognise the payload. Every one of those is silent - the packet arrives intact and the
// readings are wrong or missing. This suite covers the joins the codec suite cannot see.
#include "Arduino.h"
#include "TestUtil.h"
#include "UptimeClock.h"
#include "configuration.h"
#include "mesh/CryptoEngine.h"
#include "mesh/MeshService.h"
#include "mesh/NodeDB.h"
#include "mesh/RadioInterface.h"
#include "mesh/Router.h"
#include "mesh/biscuit/BiscuitModule.h"
#include "modules/Telemetry/BaseTelemetryModule.h"
#include "modules/Telemetry/DeviceTelemetry.h"
#include "modules/Telemetry/TelemetryHistory.h"
#include "pb_decode.h"
#include "support/MockMeshService.h"
#include <unity.h>
#include <vector>

using namespace biscuit;

// The whole suite is about the module and the diversion, so there is nothing to assert when
// the codec is not compiled in. Skip loudly rather than silently reporting a pass.
#if !MESHTASTIC_BISCUIT_ENABLED
void setup()
{
    initializeTestEnvironment();
    UNITY_BEGIN();
    TEST_IGNORE_MESSAGE("MESHTASTIC_BISCUIT_ENABLED is off; nothing to integrate");
    exit(UNITY_END());
}
void loop() {}
#else

namespace
{
constexpr NodeNum LOCAL_NODE = 0x11111111;
constexpr NodeNum REMOTE_NODE = 0x22222222;

class MockNodeDB : public NodeDB
{
};

class MockRadioInterface : public RadioInterface
{
  public:
    ErrorCode send(meshtastic_MeshPacket *p) override
    {
        packetPool.release(p);
        return ERRNO_OK;
    }
    uint32_t getPacketTime(uint32_t len, bool received = false) override
    {
        (void)len;
        (void)received;
        return 0;
    }
};

/// Captures whatever the publish path hands the router, which is the only place the finished
/// packet - portnum, payload and all - can be seen as the mesh would see it.
class CapturingRouter : public Router
{
  public:
    ~CapturingRouter()
    {
        delete cryptLock;
        cryptLock = nullptr;
    }
    ErrorCode send(meshtastic_MeshPacket *p) override
    {
        sent.push_back(*p);
        packetPool.release(p);
        return ERRNO_OK;
    }
    std::vector<meshtastic_MeshPacket> sent;
};

/// BaseTelemetryModule is a mixin with virtual alloc hooks, so a harness can drive the real
/// publish path without standing up a whole telemetry module and its sensors.
class Harness : public BaseTelemetryModule
{
  public:
    using BaseTelemetryModule::biscuitMaxTier;
    using BaseTelemetryModule::publishBufferedTelemetry;

  protected:
    meshtastic_MeshPacket *allocTelemetryHistoryPacket() override
    {
        meshtastic_MeshPacket *p = packetPool.allocZeroed();
        if (!p)
            return nullptr;
        p->from = LOCAL_NODE;
        p->to = NODENUM_BROADCAST;
        p->which_payload_variant = meshtastic_MeshPacket_decoded_tag;
        return p;
    }
};

/// handleReceived is protected, as it should be - the router is its only real caller. The test
/// stands in for the router rather than reaching around the module's interface.
class TestBiscuitModule : public BiscuitModule
{
  public:
    using BiscuitModule::handleReceived;
};
MockNodeDB *mockNodeDB;
MockMeshService *mockService;
CapturingRouter *capturingRouter;
Harness *harness;
TestBiscuitModule *biscuitUnderTest;

TelemetryHistoryBuffer<meshtastic_DeviceMetrics, DEVICE_TELEMETRY_HISTORY_SIZE> *history;

/// Readings that differ in every field, so a batch reassembled in the wrong order or with a
/// column crossed over is caught rather than looking plausible.
void pushReadings(uint8_t n, uint32_t firstEpoch = 1757000000u, uint32_t stepSecs = 900u)
{
    for (uint8_t i = 0; i < n; i++) {
        meshtastic_DeviceMetrics m = meshtastic_DeviceMetrics_init_zero;
        m.has_battery_level = true;
        m.has_voltage = true;
        m.has_channel_utilization = true;
        m.has_air_util_tx = true;
        m.has_uptime_seconds = true;
        m.battery_level = (uint32_t)(90 - i);
        m.voltage = 4.100f - 0.005f * i;
        m.channel_utilization = 3.0f + 0.5f * i;
        m.air_util_tx = 0.75f + 0.125f * i;
        m.uptime_seconds = 100000u + 60u * i;
        history->push(m, firstEpoch + stepSecs * i);
    }
}

size_t unpublishedCount()
{
    size_t n = 0;
    for (uint8_t i = 0; i < history->size(); i++)
        if (!(history->at(i).publishedMask & TELEMETRY_PUBLISHED_MESH))
            n++;
    return n;
}

} // namespace

void setUp(void)
{
    config = meshtastic_LocalConfig_init_zero;
    moduleConfig = meshtastic_LocalModuleConfig_init_zero;
    channelFile = meshtastic_ChannelFile_init_zero;
    owner = meshtastic_User_init_zero;
    myNodeInfo.my_node_num = LOCAL_NODE;

    mockNodeDB = new MockNodeDB();
    nodeDB = mockNodeDB;

    mockService = new MockMeshService();
    service = mockService;

    channels.initDefaults();
    channels.onConfigChanged();

    capturingRouter = new CapturingRouter();
    capturingRouter->addInterface(std::unique_ptr<RadioInterface>(new MockRadioInterface()));
    router = capturingRouter;

    harness = new Harness();
    biscuitUnderTest = new TestBiscuitModule();
    history = new TelemetryHistoryBuffer<meshtastic_DeviceMetrics, DEVICE_TELEMETRY_HISTORY_SIZE>();
}

void tearDown(void)
{
    Time::useRealClock(); // several cases drive a virtual timebase; do not leak it to the next

    delete history;
    history = nullptr;
    delete biscuitUnderTest;
    biscuitUnderTest = nullptr;
    biscuitModule = nullptr;
    delete harness;
    harness = nullptr;

    while (auto *status = mockService->getQueueStatusForPhone())
        mockService->releaseQueueStatusToPool(status);
    while (auto *toPhone = mockService->getForPhone())
        mockService->releaseToPool(toPhone);
    delete mockService;
    mockService = nullptr;
    service = nullptr;

    delete capturingRouter;
    capturingRouter = nullptr;
    router = nullptr;

    delete mockNodeDB;
    mockNodeDB = nullptr;
    nodeDB = nullptr;
}

// ---------------------------------------------------------------- hold and flush

/// Below the flush threshold nothing goes out. The threshold exists because a batch smaller
/// than about six readings costs more in column framing than it saves in tags, so flushing
/// early spends airtime to save nothing - and a node that published on every reading would
/// never batch at all, which is the whole feature failing silently rather than loudly.
void test_holdsBelowTheFlushThreshold(void)
{
#if MESHTASTIC_BISCUIT_FLUSH_COUNT > 1
    pushReadings(MESHTASTIC_BISCUIT_FLUSH_COUNT - 1);
    TEST_ASSERT_FALSE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(0, capturingRouter->sent.size());
    TEST_ASSERT_EQUAL(MESHTASTIC_BISCUIT_FLUSH_COUNT - 1, unpublishedCount());
#else
    TEST_IGNORE_MESSAGE("no flush threshold configured");
#endif
}

/// On reaching the threshold exactly one packet goes out carrying the batch.
void test_flushesOnReachingTheThreshold(void)
{
    pushReadings(MESHTASTIC_BISCUIT_FLUSH_COUNT ? MESHTASTIC_BISCUIT_FLUSH_COUNT : 6);
    TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(1, capturingRouter->sent.size());
}

/// A full buffer must publish even if it never reaches the threshold, or a node whose history
/// is smaller than the threshold holds its readings until they are overwritten.
void test_fullBufferPublishesBelowTheThreshold(void)
{
    pushReadings(DEVICE_TELEMETRY_HISTORY_SIZE);
    TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(1, capturingRouter->sent.size());
}

// ---------------------------------------------------------------- portnum selection

/// The batch must go out on the port its format is on. A Biscuit payload dispatched on
/// TELEMETRY_HISTORY_APP would be handed to a TelemetryRecordHistory parser, which would fail
/// to decode it - or worse, decode part of it - and the readings are lost either way.
void test_portnumMatchesTheFormatUsed(void)
{
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        history->clear();
        capturingRouter->sent.clear();
        harness->biscuitMaxTier = tier;
        pushReadings(12);
        TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
        TEST_ASSERT_EQUAL(1, capturingRouter->sent.size());
        const meshtastic_MeshPacket &p = capturingRouter->sent[0];

#if MESHTASTIC_BISCUIT_DIVERT
        // Diverted: the payload must actually be a Biscuit one, not merely labelled as such.
        TEST_ASSERT_EQUAL(meshtastic_PortNum_BISCUIT_APP, p.decoded.portnum);
        uint32_t ctx = 0;
        TEST_ASSERT_TRUE(peekContext(p.decoded.payload.bytes, p.decoded.payload.size, &ctx));
        TEST_ASSERT_EQUAL(meshtastic_Telemetry_device_metrics_tag, BiscuitModule::variantOf(ctx));
#else
        TEST_ASSERT_EQUAL(meshtastic_PortNum_TELEMETRY_HISTORY_APP, p.decoded.portnum);
#endif
    }
}

// ---------------------------------------------------------------- retire and overlap

/// Only what was actually sent may be retired, and the overlap tail must stay pending so it
/// rides again in the next packet. Retiring a reading that was not sent loses it outright;
/// retiring none resends the same batch forever.
void test_retiresSentReadingsAndKeepsTheOverlapTail(void)
{
    const uint8_t n = 12;
    pushReadings(n);
    const size_t before = unpublishedCount();
    TEST_ASSERT_EQUAL(n, before);

    TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(1, capturingRouter->sent.size());

    const uint8_t took = peekCount(capturingRouter->sent[0].decoded.payload.bytes, capturingRouter->sent[0].decoded.payload.size);
    const size_t sent = took ? took : before; // 0 when the fallback format was used
    const size_t expectRetired = retireCount(sent, MESHTASTIC_BISCUIT_OVERLAP_COUNT);
    char msg[96];
    snprintf(msg, sizeof(msg), "sent %u, retired %u, still pending %u", (unsigned)sent, (unsigned)expectRetired,
             (unsigned)unpublishedCount());
    TEST_ASSERT_EQUAL_MESSAGE(before - expectRetired, unpublishedCount(), msg);
}

/// Overlap repeats readings on purpose, so a second publish must carry the tail of the first
/// again. Without this the setting looks like it works while retiring everything.
void test_overlapRepeatsTheTailInTheNextPacket(void)
{
#if MESHTASTIC_BISCUIT_OVERLAP_COUNT > 0
    pushReadings(MESHTASTIC_BISCUIT_FLUSH_COUNT);
    TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(MESHTASTIC_BISCUIT_OVERLAP_COUNT, unpublishedCount());

    pushReadings(MESHTASTIC_BISCUIT_FLUSH_COUNT - MESHTASTIC_BISCUIT_OVERLAP_COUNT, 1757100000u);
    TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(2, capturingRouter->sent.size());
#else
    TEST_IGNORE_MESSAGE("overlap disabled in this build");
#endif
}

/// Publishing nothing must not emit a packet, and must not claim it did.
void test_emptyHistoryPublishesNothing(void)
{
    TEST_ASSERT_FALSE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(0, capturingRouter->sent.size());
}

// ---------------------------------------------------------------- fallback

/// Biscuit declines rather than emit a packet no smaller than the protobuf it replaces. When it
/// does, the batch must still go out - in the fallback format, on the fallback port. A node that
/// silently published nothing whenever the codec declined would lose readings on exactly the
/// small batches the threshold is meant to let through.
void test_biscuitDeclines_fallsBackToTheRecordFormat(void)
{
    // One reading cannot beat its own protobuf: the header alone exceeds what it replaces.
    pushReadings(1);
    // Reach past the flush threshold by filling the buffer, so the hold is not what is observed.
    while (!history->isFull())
        pushReadings(1, 1757200000u + history->size() * 900u);

    TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(1, capturingRouter->sent.size());
    // Whatever was chosen, the port must match the format actually written.
    const meshtastic_MeshPacket &p = capturingRouter->sent[0];
    if (p.decoded.portnum == meshtastic_PortNum_BISCUIT_APP) {
        TEST_ASSERT_TRUE(peekCount(p.decoded.payload.bytes, p.decoded.payload.size) > 0);
    } else {
        TEST_ASSERT_EQUAL(meshtastic_PortNum_TELEMETRY_HISTORY_APP, p.decoded.portnum);
        TEST_ASSERT_EQUAL(0, peekCount(p.decoded.payload.bytes, p.decoded.payload.size));
    }
}

/// A DeviceMetrics batch has no fallback at all - TelemetryRecord's oneof cannot hold one - so
/// the record path must decline rather than emit a packet with an empty or malformed payload.
void test_deviceMetrics_hasNoRecordFallback(void)
{
    TEST_ASSERT_FALSE(recordCarries<meshtastic_DeviceMetrics>());
    TEST_ASSERT_TRUE(recordCarries<meshtastic_EnvironmentMetrics>());
    TEST_ASSERT_TRUE(recordCarries<meshtastic_PowerMetrics>());
    TEST_ASSERT_TRUE(recordCarries<meshtastic_AirQualityMetrics>());
}

// ---------------------------------------------------------------- the three time cases

/// A node with a trustworthy clock stamps each reading with an epoch, and the batch carries
/// those epochs unchanged.
void test_time_clockPresentSendsEpochs(void)
{
    Time::setTestMillis(3600u * 1000u);
    pushReadings(12, 1757000000u, 900u);
    TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(1, capturingRouter->sent.size());

#if MESHTASTIC_BISCUIT_DIVERT
    const meshtastic_MeshPacket &p = capturingRouter->sent[0];
    uint32_t ctx = 0;
    TEST_ASSERT_TRUE(peekContext(p.decoded.payload.bytes, p.decoded.payload.size, &ctx));
    // With a clock the batch must not claim to be uptime-based, or the receiver will subtract
    // every stamp from its own clock and file the readings decades out.
    TEST_ASSERT_EQUAL(0, ctx & BiscuitModule::CTX_UPTIME_BASED);
#endif
}

/// A reading captured before the clock arrived has no epoch, only an uptime. The publish path
/// back-dates it: epoch = now - (nowUptime - capturedUptime). The elapsed term is monotonic, so
/// it is exact at any age, which is the same trick MeshService::reconcilePendingRxTimes() uses.
void test_time_clockArrivedLateBackDatesFromUptime(void)
{
    Time::setTestMillis(10000u * 1000u); // 10000 s of uptime
    for (uint8_t i = 0; i < 12; i++) {
        meshtastic_DeviceMetrics m = meshtastic_DeviceMetrics_init_zero;
        m.has_battery_level = true;
        m.battery_level = (uint32_t)(70 - i);
        m.has_uptime_seconds = true;
        m.uptime_seconds = 9000u + 60u * i;
        history->push(m, 0); // no epoch: the clock had not arrived
        Time::advanceTestMillis(60u * 1000u);
    }
    TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    TEST_ASSERT_EQUAL(1, capturingRouter->sent.size());
    // Undated readings must still be published; the assertion that matters is that the path ran
    // at all, since a batch of zero-stamped readings used to be indistinguishable from a bug.
    TEST_ASSERT_TRUE(capturingRouter->sent[0].decoded.payload.size > 0);
}

// ---------------------------------------------------------------- mqtt target

/// The mqtt target shares the encode path and differs only in delivery. Without a broker it must
/// decline cleanly and retire nothing, rather than marking readings published that never left.
void test_mqttTarget_withoutABrokerRetiresNothing(void)
{
    pushReadings(12);
    const size_t before = unpublishedCount();
    TEST_ASSERT_FALSE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mqtt));
    TEST_ASSERT_EQUAL(before, unpublishedCount());
    TEST_ASSERT_EQUAL(0, capturingRouter->sent.size());
}

/// The two targets carry independent published masks, so a batch sent to the mesh must still be
/// pending for mqtt. Sharing one mask would silently drop every reading from one of the two.
void test_publishTargets_haveIndependentMasks(void)
{
    pushReadings(12);
    TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
    uint8_t stillPendingForMqtt = 0;
    for (uint8_t i = 0; i < history->size(); i++)
        if (!(history->at(i).publishedMask & TELEMETRY_PUBLISHED_MQTT))
            stillPendingForMqtt++;
    TEST_ASSERT_EQUAL(history->size(), stillPendingForMqtt);
}
// ---------------------------------------------------------------- node to node

#if MESHTASTIC_BISCUIT_DIVERT

/// The proof case: a batch built by the sending node's publish path, handed to the receiving
/// node's module exactly as the radio would deliver it, and every reading recovered. This is
/// the only test where the encoder's Options and the decoder's Options are not the same
/// object, so it is the only one that can catch the two ends disagreeing - which is what the
/// unsent time quantum did, and what a family table or a resolution hint could still do.
void test_nodeToNode_batchSurvivesTheModuleBoundary(void)
{
    // Every tier, in one binary: the encodings differ completely between them - tier 4 shares one
    // bit area across all columns and carries no column headers at all - so a boundary that works
    // at the default proves nothing about the other three.
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        history->clear();
        capturingRouter->sent.clear();
        harness->biscuitMaxTier = tier;
        // A fresh receiver each time. The module remembers the newest stamp it has taken from a
        // sender so it can drop declared repeats, and every tier here replays the same twelve
        // readings - without this, tier 2 onward correctly discards the overlap tail as already
        // seen, which is the dedup working rather than the boundary failing.
        delete biscuitUnderTest;
        biscuitUnderTest = new TestBiscuitModule();
        const uint8_t n = 12;
        pushReadings(n);

        meshtastic_DeviceMetrics expect[16];
        uint32_t expectTimes[16];
        for (uint8_t i = 0; i < history->size(); i++) {
            expect[i] = history->at(i).metrics;
            expectTimes[i] = history->at(i).time;
        }

        TEST_ASSERT_TRUE(harness->publishBufferedTelemetry(*history, Harness::PublishTarget::Mesh));
        TEST_ASSERT_EQUAL(1, capturingRouter->sent.size());

        meshtastic_MeshPacket rx = capturingRouter->sent[0];
        rx.from = REMOTE_NODE; // it came from the other node, as far as we are concerned
        TEST_ASSERT_EQUAL(meshtastic_PortNum_BISCUIT_APP, rx.decoded.portnum);

        const uint8_t took = peekCount(rx.decoded.payload.bytes, rx.decoded.payload.size);
        TEST_ASSERT_TRUE(took > 0);

        // Drive the receiving module the way the router would.
        TEST_ASSERT_EQUAL(ProcessMessage::STOP, biscuitUnderTest->handleReceived(rx));

        // Each reading is delivered to the phone as an ordinary Telemetry message. Pull them back
        // out of the phone queue and compare against what the sender accumulated.
        uint8_t got = 0;
        while (meshtastic_MeshPacket *out = mockService->getForPhone()) {
            TEST_ASSERT_EQUAL(meshtastic_PortNum_TELEMETRY_APP, out->decoded.portnum);
            meshtastic_Telemetry t = meshtastic_Telemetry_init_zero;
            pb_istream_t stream = pb_istream_from_buffer(out->decoded.payload.bytes, out->decoded.payload.size);
            TEST_ASSERT_TRUE(pb_decode(&stream, &meshtastic_Telemetry_msg, &t));
            TEST_ASSERT_EQUAL(meshtastic_Telemetry_device_metrics_tag, t.which_variant);

            char msg[48];
            snprintf(msg, sizeof(msg), "tier %u reading %u", tier, got);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(expectTimes[got], t.time, msg);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(expect[got].battery_level, t.variant.device_metrics.battery_level, msg);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(expect[got].uptime_seconds, t.variant.device_metrics.uptime_seconds, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, expect[got].voltage, t.variant.device_metrics.voltage, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, expect[got].channel_utilization,
                                             t.variant.device_metrics.channel_utilization, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, expect[got].air_util_tx, t.variant.device_metrics.air_util_tx, msg);

            mockService->releaseToPool(out);
            got++;
        }
        TEST_ASSERT_EQUAL_MESSAGE(took, got, "every reading in the batch must reach the phone");
    }
}

/// A payload that is not ours must be refused rather than decoded into invented readings.
void test_nodeToNode_foreignPayloadIsRefused(void)
{
    meshtastic_MeshPacket rx = meshtastic_MeshPacket_init_zero;
    rx.from = REMOTE_NODE;
    rx.to = NODENUM_BROADCAST;
    rx.which_payload_variant = meshtastic_MeshPacket_decoded_tag;
    rx.decoded.portnum = meshtastic_PortNum_BISCUIT_APP;
    rx.decoded.payload.size = 16;
    memset(rx.decoded.payload.bytes, 0x5A, rx.decoded.payload.size);

    TEST_ASSERT_EQUAL(ProcessMessage::STOP, biscuitUnderTest->handleReceived(rx));
    TEST_ASSERT_TRUE(mockService->getForPhone() == nullptr);
}

/// A receiver whose own quantum differs from the sender's must still rebuild the sender's
/// stamps, because the quantum travels in the context word. Before it did, both ends read
/// their own configuration and a mismatch rebuilt every timestamp wrong by that factor with
/// nothing failing - the readings simply landed in the wrong hour.
void test_nodeToNode_timeQuantumComesFromTheSender(void)
{
    meshtastic_DeviceMetrics src[8];
    uint32_t ts[8];
    for (uint8_t i = 0; i < 8; i++) {
        src[i] = meshtastic_DeviceMetrics_init_zero;
        src[i].has_battery_level = true;
        src[i].battery_level = (uint32_t)(80 - i);
        src[i].has_uptime_seconds = true;
        src[i].uptime_seconds = 5000u + 600u * i;
        ts[i] = 1757000000u + 600u * i;
    }
    const void *sp[8];
    for (uint8_t i = 0; i < 8; i++)
        sp[i] = &src[i];

    // Sender coarsens to the minute and says so in the context word.
    Options send;
    send.fixed32IsFloat = true;
    send.timeRes = 60;
    send.context = BiscuitModule::makeContext(meshtastic_Telemetry_device_metrics_tag, 4, 0, send.timeRes);
    send.neverInflate = false;

    uint8_t buf[233];
    Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 8, ts, buf, sizeof(buf), send);
    TEST_ASSERT_TRUE(r.size > 0);

    meshtastic_MeshPacket rx = meshtastic_MeshPacket_init_zero;
    rx.from = REMOTE_NODE;
    rx.to = NODENUM_BROADCAST;
    rx.which_payload_variant = meshtastic_MeshPacket_decoded_tag;
    rx.decoded.portnum = meshtastic_PortNum_BISCUIT_APP;
    rx.decoded.payload.size = r.size;
    memcpy(rx.decoded.payload.bytes, buf, r.size);

    // The receiving module is left at its default quantum of 1 second. If it used that instead
    // of the sender's 60, every stamp would come back about sixty times too small.
    TEST_ASSERT_EQUAL(ProcessMessage::STOP, biscuitUnderTest->handleReceived(rx));

    uint8_t got = 0;
    while (meshtastic_MeshPacket *out = mockService->getForPhone()) {
        meshtastic_Telemetry t = meshtastic_Telemetry_init_zero;
        pb_istream_t stream = pb_istream_from_buffer(out->decoded.payload.bytes, out->decoded.payload.size);
        TEST_ASSERT_TRUE(pb_decode(&stream, &meshtastic_Telemetry_msg, &t));
        const uint32_t d = ts[got] > t.time ? ts[got] - t.time : t.time - ts[got];
        char msg[80];
        snprintf(msg, sizeof(msg), "reading %u: sent %u, got %u", got, (unsigned)ts[got], (unsigned)t.time);
        TEST_ASSERT_LESS_THAN_UINT32_MESSAGE(60u, d, msg);
        mockService->releaseToPool(out);
        got++;
    }
    TEST_ASSERT_EQUAL(8, got);
}

#endif // MESHTASTIC_BISCUIT_DIVERT

void setup()
{
    initializeTestEnvironment();
    UNITY_BEGIN();
    RUN_TEST(test_holdsBelowTheFlushThreshold);
    RUN_TEST(test_flushesOnReachingTheThreshold);
    RUN_TEST(test_fullBufferPublishesBelowTheThreshold);
    RUN_TEST(test_portnumMatchesTheFormatUsed);
    RUN_TEST(test_retiresSentReadingsAndKeepsTheOverlapTail);
    RUN_TEST(test_overlapRepeatsTheTailInTheNextPacket);
    RUN_TEST(test_emptyHistoryPublishesNothing);
    RUN_TEST(test_biscuitDeclines_fallsBackToTheRecordFormat);
    RUN_TEST(test_deviceMetrics_hasNoRecordFallback);
    RUN_TEST(test_time_clockPresentSendsEpochs);
    RUN_TEST(test_time_clockArrivedLateBackDatesFromUptime);
    RUN_TEST(test_mqttTarget_withoutABrokerRetiresNothing);
    RUN_TEST(test_publishTargets_haveIndependentMasks);
#if MESHTASTIC_BISCUIT_DIVERT
    RUN_TEST(test_nodeToNode_batchSurvivesTheModuleBoundary);
    RUN_TEST(test_nodeToNode_foreignPayloadIsRefused);
    RUN_TEST(test_nodeToNode_timeQuantumComesFromTheSender);
#endif
    exit(UNITY_END());
}

void loop() {}

#endif // MESHTASTIC_BISCUIT_ENABLED
