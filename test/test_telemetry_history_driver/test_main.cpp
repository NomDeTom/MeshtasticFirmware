// The accumulator wiring, driven through the real telemetry modules.
//
// test_biscuit_integration hand-fills a TelemetryHistoryBuffer and drives publishBufferedTelemetry
// from there. That proves the publish path but says nothing about whether a module actually puts
// its readings into the buffer - the wiring added to DeviceTelemetry, EnvironmentTelemetry and
// PowerTelemetry is a single push() call each, and a batch that is always empty publishes nothing
// while looking entirely healthy. This suite constructs the modules for real and asserts that
// producing a reading fills the history.
//
// It also covers TelemetryHistoryBuffer itself, which every accumulator depends on and which had
// no direct tests: eviction when full, per-channel published masks, and the uptime stamp that
// dates a reading taken before the clock was trustworthy.
#include "Arduino.h"
#include "BiscuitCompare.h"
#include "NodeStatus.h"
#include "PowerStatus.h"
#include "TestUtil.h"
#include "UptimeClock.h"
#include "airtime.h"
#include "configuration.h"
#include "mesh/CryptoEngine.h"
#include "mesh/MeshService.h"
#include "mesh/NodeDB.h"
#include "mesh/RadioInterface.h"
#include "mesh/Router.h"
#include "modules/Telemetry/DeviceTelemetry.h"
#include "modules/Telemetry/TelemetryHistory.h"
#include "support/MockMeshService.h"
#include <unity.h>

namespace
{
constexpr NodeNum LOCAL_NODE = 0x11111111;

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

/// sendTelemetry() is protected and history is private, which is right - the module owns both.
/// The test stands in for runOnce() rather than reaching around the interface.
class DriverModule : public DeviceTelemetryModule
{
  public:
    using DeviceTelemetryModule::sendTelemetry;
    const TelemetryHistoryBuffer<meshtastic_DeviceMetrics, DEVICE_TELEMETRY_HISTORY_SIZE> &readHistory() const { return history; }
};

MockNodeDB *mockNodeDB;
MockMeshService *mockService;
CapturingRouter *capturingRouter;
DriverModule *driver;

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

    // getDeviceTelemetry() reads all three of these; without them the module cannot produce a
    // reading at all, which is why the accumulator wiring had gone untested. Assigned rather
    // than conditionally created: a default-constructed PowerStatus reports no battery, and
    // getDeviceTelemetry then substitutes the USB sentinel for the charge level, which looks
    // like a real reading and silently defeats the value assertions below.
    static meshtastic::NodeStatus testNodeStatus(1, 1);
    static AirTime testAirTime;
    static meshtastic::PowerStatus testPowerStatus(meshtastic::OptTrue, meshtastic::OptFalse, meshtastic::OptFalse, 4100, 85);
    nodeStatus = &testNodeStatus;
    airTime = &testAirTime;
    powerStatus = &testPowerStatus;

    Time::setTestMillis(3600u * 1000u);
    driver = new DriverModule();
}

void tearDown(void)
{
    Time::useRealClock();

    delete driver;
    driver = nullptr;

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

// ---------------------------------------------------------------- the wiring

/// One reading produced by the module must land in its history. This is the assertion the
/// integration suite cannot make, because it fills the buffer itself: if the push() were dropped
/// from sendTelemetry(), every batch would be empty and publishBufferedTelemetry would simply
/// return false forever, which looks exactly like a node with nothing to report.
void test_sendTelemetry_pushesIntoHistory(void)
{
    TEST_ASSERT_TRUE(driver->readHistory().isEmpty());
    TEST_ASSERT_TRUE(driver->sendTelemetry());
    TEST_ASSERT_EQUAL_MESSAGE(1, driver->readHistory().size(), "sendTelemetry did not accumulate");
}

/// Readings accumulate rather than overwrite, and arrive oldest-first, which is the order every
/// column in a batch is built from. Reversed, the deltas would still encode and decode - they
/// would just describe the series backwards.
void test_repeatedSends_accumulateInOrder(void)
{
    for (uint8_t i = 0; i < 5; i++) {
        TEST_ASSERT_TRUE(driver->sendTelemetry());
        Time::advanceTestMillis(60u * 1000u);
    }
    const auto &h = driver->readHistory();
    TEST_ASSERT_EQUAL(5, h.size());
    for (uint8_t i = 1; i < h.size(); i++) {
        char msg[64];
        snprintf(msg, sizeof(msg), "reading %u uptime %u vs %u", i, (unsigned)h.at(i).uptimeSecs,
                 (unsigned)h.at(i - 1).uptimeSecs);
        TEST_ASSERT_GREATER_OR_EQUAL_UINT32_MESSAGE(h.at(i - 1).uptimeSecs, h.at(i).uptimeSecs, msg);
    }
}

/// The module's readings carry the values it just reported, not zeros. A push of a
/// default-constructed struct would satisfy every count-based assertion above and still batch
/// nothing but noise.
void test_pushedReadingCarriesTheReportedValues(void)
{
    TEST_ASSERT_TRUE(driver->sendTelemetry());
    const auto &r = driver->readHistory().at(0);
    TEST_ASSERT_TRUE(r.metrics.has_battery_level);
    TEST_ASSERT_EQUAL_UINT32(85, r.metrics.battery_level);
    TEST_ASSERT_TRUE(r.metrics.has_voltage);
    TEST_ASSERT_FLOAT_WITHIN(0.001f, 4.100f, r.metrics.voltage);
    TEST_ASSERT_TRUE(r.metrics.has_uptime_seconds);
}

/// Every reading is stamped with uptime whether or not a wall clock exists, because that stamp
/// is what lets a batch be dated later - epoch = now - (nowUptime - capturedUptime). A reading
/// with no uptime cannot be dated by anything.
void test_everyReadingIsStampedWithUptime(void)
{
    Time::setTestMillis(12345u * 1000u);
    TEST_ASSERT_TRUE(driver->sendTelemetry());
    TEST_ASSERT_EQUAL_UINT32(12345u, driver->readHistory().at(0).uptimeSecs);
}

// ---------------------------------------------------------------- the buffer itself

/// A full buffer evicts the oldest, which is what makes the flush threshold's full-buffer escape
/// necessary: past this point a reading that is never published is lost rather than delayed.
void test_buffer_evictsOldestWhenFull(void)
{
    TelemetryHistoryBuffer<meshtastic_DeviceMetrics, 4> buf;
    for (uint8_t i = 0; i < 6; i++) {
        meshtastic_DeviceMetrics m = meshtastic_DeviceMetrics_init_zero;
        m.has_battery_level = true;
        m.battery_level = i;
        buf.push(m, 1757000000u + i);
    }
    TEST_ASSERT_TRUE(buf.isFull());
    TEST_ASSERT_EQUAL(4, buf.size());
    // The two oldest are gone; what remains is 2,3,4,5 oldest-first, and each record must be
    // the reading that was pushed rather than merely carrying the right battery level.
    TEST_ASSERT_EQUAL_UINT32(2, buf.at(0).metrics.battery_level);
    TEST_ASSERT_EQUAL_UINT32(5, buf.at(3).metrics.battery_level);
    for (uint8_t i = 0; i < 4; i++) {
        meshtastic_DeviceMetrics want = meshtastic_DeviceMetrics_init_zero;
        want.has_battery_level = true;
        want.battery_level = (uint32_t)(i + 2);
        char msg[48];
        snprintf(msg, sizeof(msg), "buffer slot %u", i);
        biscuitcmp::assertSameReading(&meshtastic_DeviceMetrics_msg, &want, &buf.at(i).metrics, msg);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(1757000000u + i + 2, buf.at(i).time, msg);
    }
}

/// isFull is what the flush threshold consults, so it must mean capacity and not merely
/// non-empty. It reading as "has anything" is precisely the bug that made the threshold inert.
void test_buffer_isFullMeansCapacity(void)
{
    TelemetryHistoryBuffer<meshtastic_DeviceMetrics, 4> buf;
    meshtastic_DeviceMetrics m = meshtastic_DeviceMetrics_init_zero;
    for (uint8_t i = 0; i < 3; i++) {
        buf.push(m, 1757000000u + i);
        TEST_ASSERT_FALSE(buf.isFull());
    }
    buf.push(m, 1757000004u);
    TEST_ASSERT_TRUE(buf.isFull());
}

/// The mesh and mqtt masks are independent, so publishing to one leaves the reading pending for
/// the other. One shared flag would silently drop every reading from whichever published second.
void test_buffer_publishedMasksAreIndependent(void)
{
    TelemetryHistoryBuffer<meshtastic_DeviceMetrics, 4> buf;
    meshtastic_DeviceMetrics m = meshtastic_DeviceMetrics_init_zero;
    buf.push(m, 1757000000u);
    buf.markPublished(0, TELEMETRY_PUBLISHED_MESH);
    TEST_ASSERT_TRUE(buf.at(0).publishedMask & TELEMETRY_PUBLISHED_MESH);
    TEST_ASSERT_FALSE(buf.at(0).publishedMask & TELEMETRY_PUBLISHED_MQTT);
}

/// A reading pushed without a wall clock keeps time == 0 rather than inventing an epoch, so the
/// publish path can tell "undated" from "dated 1970" and back-date it from uptime instead.
void test_buffer_undatedReadingKeepsTimeZero(void)
{
    TelemetryHistoryBuffer<meshtastic_DeviceMetrics, 4> buf;
    meshtastic_DeviceMetrics m = meshtastic_DeviceMetrics_init_zero;
    Time::setTestMillis(500u * 1000u);
    buf.push(m, 0);
    TEST_ASSERT_EQUAL_UINT32(0, buf.at(0).time);
    TEST_ASSERT_EQUAL_UINT32(500u, buf.at(0).uptimeSecs);
}

void setup()
{
    initializeTestEnvironment();
    UNITY_BEGIN();
    RUN_TEST(test_sendTelemetry_pushesIntoHistory);
    RUN_TEST(test_repeatedSends_accumulateInOrder);
    RUN_TEST(test_pushedReadingCarriesTheReportedValues);
    RUN_TEST(test_everyReadingIsStampedWithUptime);
    RUN_TEST(test_buffer_evictsOldestWhenFull);
    RUN_TEST(test_buffer_isFullMeansCapacity);
    RUN_TEST(test_buffer_publishedMasksAreIndependent);
    RUN_TEST(test_buffer_undatedReadingKeepsTimeZero);
    exit(UNITY_END());
}

void loop() {}
