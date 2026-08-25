#include "MeshTypes.h"
#include "TestUtil.h"
#include <unity.h>

#if ARCH_PORTDUINO // portduino_config.maxtophone is what sizes the queue under test

#include "UptimeClock.h"
#include "configuration.h"
#include "gps/RTC.h"
#include "mesh/MeshService.h"
#include "mesh/NodeDB.h"
#include "platform/portduino/PortduinoGlue.h"
#include <cstdio>
#include <cstdlib>
#include <vector>

// Queue depth for the suite. MAX_RX_TOPHONE resolves to portduino_config.maxtophone, read when
// MeshService constructs its queue.
static const int TEST_QUEUE_LEN = 4;

static MeshService *testService = nullptr;
static MeshService *savedService = nullptr;
static int savedMaxToPhone = 0;
static meshtastic_Config_DeviceConfig_RebroadcastMode savedRebroadcastMode;

static meshtastic_MeshPacket basePacket(uint32_t id)
{
    meshtastic_MeshPacket p = meshtastic_MeshPacket_init_zero;
    p.from = 0x11223344;
    p.to = NODENUM_BROADCAST;
    p.id = id;
    return p;
}

static void sendPacket(const meshtastic_MeshPacket &src)
{
    meshtastic_MeshPacket *p = packetPool.allocCopy(src);
    TEST_ASSERT_NOT_NULL(p);
    service->sendToPhone(p);
}

static void send(uint32_t id, meshtastic_PortNum portnum, uint32_t requestId = 0)
{
    meshtastic_MeshPacket src = basePacket(id);
    src.which_payload_variant = meshtastic_MeshPacket_decoded_tag;
    src.decoded.portnum = portnum;
    src.decoded.request_id = requestId;
    sendPacket(src);
}

static void fillWith(meshtastic_PortNum portnum, uint32_t firstId)
{
    for (int i = 0; i < TEST_QUEUE_LEN; i++)
        send(firstId + i, portnum);
}

/// Drain the queue, returning the delivered packet ids in order.
static std::vector<uint32_t> drainIds()
{
    std::vector<uint32_t> ids;
    while (meshtastic_MeshPacket *p = service->getForPhone()) {
        ids.push_back(p->id);
        service->releaseToPool(p);
    }
    return ids;
}

// Step the injected clock the way the firmware does - serviceMonotonic() must run at least once
// per ~49.7-day window or the carry misses a wrap.
static void advanceAndService(uint32_t deltaMs)
{
    Time::advanceTestMillis(deltaMs);
    Time::serviceMonotonic();
}

static void assertIds(const std::vector<uint32_t> &expected, const char *what)
{
    const std::vector<uint32_t> actual = drainIds();
    TEST_ASSERT_EQUAL_INT_MESSAGE((int)expected.size(), (int)actual.size(), what);
    for (size_t i = 0; i < expected.size(); i++)
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(expected[i], actual[i], what);
}

// An ACK/NAK is the phone's only delivery confirmation, so it must displace the oldest packet
// rather than be dropped when sustained downlink keeps the queue full.
static void test_routing_response_admitted_when_queue_full(void)
{
    fillWith(meshtastic_PortNum_TELEMETRY_APP, 1);
    send(100, meshtastic_PortNum_ROUTING_APP, /*requestId=*/7);

    assertIds({2, 3, 4, 100}, "oldest telemetry should have been evicted for the routing response");
}

static void test_text_evicts_oldest_when_full(void)
{
    fillWith(meshtastic_PortNum_TELEMETRY_APP, 1);
    send(200, meshtastic_PortNum_TEXT_MESSAGE_APP);

    assertIds({2, 3, 4, 200}, "text should still evict the oldest packet");
}

static void test_low_priority_packet_still_dropped_when_full(void)
{
    fillWith(meshtastic_PortNum_TEXT_MESSAGE_APP, 1);
    send(200, meshtastic_PortNum_TELEMETRY_APP);

    assertIds({1, 2, 3, 4}, "a low-priority arrival should still be dropped on a full queue");
}

// decoded.portnum aliases encrypted.size in the payload union, so a still-encrypted packet whose
// ciphertext length happens to equal a privileged portnum must not be read as one.
static void test_encrypted_packet_is_not_classified_by_portnum(void)
{
    fillWith(meshtastic_PortNum_TELEMETRY_APP, 1);

    meshtastic_MeshPacket src = basePacket(300);
    src.which_payload_variant = meshtastic_MeshPacket_encrypted_tag;
    src.encrypted.size = meshtastic_PortNum_ROUTING_APP;
    sendPacket(src);

    assertIds({1, 2, 3, 4}, "an encrypted packet must not be classified from the aliased portnum");
}

void setUp(void)
{
    savedMaxToPhone = portduino_config.maxtophone;
    savedRebroadcastMode = config.device.rebroadcast_mode;
    portduino_config.maxtophone = TEST_QUEUE_LEN;
    config.device.rebroadcast_mode = meshtastic_Config_DeviceConfig_RebroadcastMode_ALL;

    testService = new MeshService();
    savedService = service;
    service = testService;
}

void tearDown(void)
{
    drainIds(); // the queue owns its pointers; a failed assertion longjmps past any in-test drain
    service = savedService;
    delete testService;
    testService = nullptr;
    portduino_config.maxtophone = savedMaxToPhone;
    config.device.rebroadcast_mode = savedRebroadcastMode;
}

// perhapsSetRTC() rejects anything before BUILD_EPOCH (stamped at build time) as implausible, so
// derive the test epoch rather than hardcoding one - same reason as test_uptime_clock's kTestEpoch.
#ifdef BUILD_EPOCH
static constexpr uint32_t kReconcileEpoch = (uint32_t)BUILD_EPOCH + 3600;
#else
static constexpr uint32_t kReconcileEpoch = 1800000000u;
#endif

// D1 regression. reconcilePendingRxTimes() dates a placeholder by the *elapsed* term
// (nowUptimeSecs - p->rx_time). Both stamps are monotonic uptime seconds, so the answer must stay
// exact at any age; the merged defect this replaced used raw millis and aliased past 49.7 days.
void test_reconcile_dates_a_placeholder_exactly_past_the_wrap()
{
    static const uint32_t kDayMs = 24u * 60 * 60 * 1000;
    static const uint32_t kElapsedDays = 60; // > 49.7, so a 32-bit millis delta would have aliased

    resetRTCStateForTests();
    Time::resetMonotonicForTests();
    Time::setTestMillis(0xFFFFFF00u); // 256 ms short of the wrap
    Time::serviceMonotonic();

    // What Router::computeRxTimeStamp() leaves behind with no trusted clock: the arrival instant in
    // uptime seconds, has_rx_time false so nothing downstream reads it as a wall clock.
    meshtastic_MeshPacket p = basePacket(0x5001);
    p.rx_time = Time::getUptimeSecs();
    p.has_rx_time = false;
    sendPacket(p);

    for (uint32_t d = 0; d < kElapsedDays; d += 10)
        advanceAndService(10 * kDayMs); // sub-wrap steps: one 60-day jump would miss the carry

    struct timeval tv = {};
    tv.tv_sec = kReconcileEpoch;
    TEST_ASSERT_EQUAL_INT(RTCSetResultSuccess, perhapsSetRTC(RTCQualityFromNet, &tv));

    service->reconcilePendingRxTimes();

    meshtastic_MeshPacket *out = service->getForPhone();
    TEST_ASSERT_NOT_NULL(out);
    TEST_ASSERT_TRUE_MESSAGE(out->has_rx_time, "reconcile must date the placeholder");
    TEST_ASSERT_EQUAL_UINT32(kReconcileEpoch - kElapsedDays * 24 * 60 * 60, out->rx_time);
    service->releaseToPool(out);

    Time::useRealClock();
    resetRTCStateForTests();
}

void setup()
{
    initializeTestEnvironment();
    UNITY_BEGIN();

    printf("\n=== toPhoneQueue overflow policy ===\n");

    RUN_TEST(test_routing_response_admitted_when_queue_full);
    RUN_TEST(test_text_evicts_oldest_when_full);
    RUN_TEST(test_low_priority_packet_still_dropped_when_full);
    RUN_TEST(test_encrypted_packet_is_not_classified_by_portnum);

    printf("\n=== rx_time placeholder reconciliation ===\n");

    RUN_TEST(test_reconcile_dates_a_placeholder_exactly_past_the_wrap);

    exit(UNITY_END());
}

void loop() {}

#else // !ARCH_PORTDUINO

void setUp(void) {}
void tearDown(void) {}

void setup()
{
    initializeTestEnvironment();
    UNITY_BEGIN();
    exit(UNITY_END());
}

void loop() {}

#endif
