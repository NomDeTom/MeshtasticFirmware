#pragma once

#include "DebugConfiguration.h"
#include "NodeDB.h"
#include "TelemetryHistory.h"
#include "configuration.h"
#include "mesh/MeshService.h"
#include "mesh/MeshTypes.h"
#include "mesh/biscuit/Biscuit.h"
#include "mesh/generated/meshtastic/telemetry.pb.h"
#include "mqtt/MQTT.h"
#include "sleep.h"

// Divert buffered telemetry through Biscuit instead of sending a TelemetryRecordHistory.
// Separate from MESHTASTIC_BISCUIT_ENABLED so the codec can be built and tested without any
// node emitting a format the network cannot yet read. Off until a receiver is deployed.
#ifndef MESHTASTIC_BISCUIT_DIVERT
#ifdef USERPREFS_BISCUIT_DIVERT
#define MESHTASTIC_BISCUIT_DIVERT USERPREFS_BISCUIT_DIVERT
#else
#define MESHTASTIC_BISCUIT_DIVERT 0
#endif
#endif

#if MESHTASTIC_BISCUIT_DIVERT && !MESHTASTIC_BISCUIT_ENABLED
#error "MESHTASTIC_BISCUIT_DIVERT needs MESHTASTIC_BISCUIT_ENABLED"
#endif

// Hold a batch until this many readings are pending, then flush. 0 publishes at every
// opportunity, which is the pre-Biscuit behaviour. Six is the floor at which batching pays:
// below it the column framing costs more than the tags it removes.
#ifndef MESHTASTIC_BISCUIT_FLUSH_COUNT
#ifdef USERPREFS_BISCUIT_FLUSH_COUNT
#define MESHTASTIC_BISCUIT_FLUSH_COUNT USERPREFS_BISCUIT_FLUSH_COUNT
#else
#define MESHTASTIC_BISCUIT_FLUSH_COUNT 6
#endif
#endif

// Readings repeated from the previous packet, so a single lost packet does not lose its
// readings outright - the next one carries the last N again. Costs airtime: a batch of
// FLUSH_COUNT carries OVERLAP_COUNT repeats and FLUSH_COUNT - OVERLAP_COUNT new readings, so
// the effective rate falls by that ratio. The receiver must discard duplicates by timestamp.
#ifndef MESHTASTIC_BISCUIT_OVERLAP_COUNT
#ifdef USERPREFS_BISCUIT_OVERLAP_COUNT
#define MESHTASTIC_BISCUIT_OVERLAP_COUNT USERPREFS_BISCUIT_OVERLAP_COUNT
#else
#define MESHTASTIC_BISCUIT_OVERLAP_COUNT 0
#endif
#endif

// Without this the newest readings are never retired and the node resends forever.
static_assert(MESHTASTIC_BISCUIT_OVERLAP_COUNT == 0 || MESHTASTIC_BISCUIT_FLUSH_COUNT == 0 ||
                  MESHTASTIC_BISCUIT_OVERLAP_COUNT < MESHTASTIC_BISCUIT_FLUSH_COUNT,
              "BISCUIT_OVERLAP_COUNT must be below BISCUIT_FLUSH_COUNT or no reading is ever retired");

#ifndef MESHTASTIC_MAX_READINGS_PER_MESH_PACKET
#define MESHTASTIC_MAX_READINGS_PER_MESH_PACKET 10
#endif

class BaseTelemetryModule
{
  public:
    /// Where a batch of buffered telemetry readings is being published to
    enum class PublishTarget { Mesh, Mqtt };

    virtual ~BaseTelemetryModule() = default;

  protected:
    bool isSensorOrRouterRole() const
    {
        return config.device.role == meshtastic_Config_DeviceConfig_Role_SENSOR ||
               config.device.role == meshtastic_Config_DeviceConfig_Role_ROUTER ||
               config.device.role == meshtastic_Config_DeviceConfig_Role_ROUTER_LATE;
    }

    /// True for a SENSOR role node with power saving enabled, the only combination that deep
    /// sleeps between telemetry broadcasts
    bool isPowerSavingSensor() const
    {
        return config.device.role == meshtastic_Config_DeviceConfig_Role_SENSOR && config.power.is_power_saving;
    }

    /**
     * Call while a deep sleep is pending (sleepOnNextExecution): the telemetry packet queued by
     * sendTelemetry() goes out asynchronously, and sleeping while it is still queued or on air
     * truncates the transmission. Returns true if the caller should reschedule in
     * PREFLIGHT_SLEEP_RETRY_MS and check again. Bounded by MAX_PREFLIGHT_SLEEP_DEFERRALS so a
     * busy mesh can't keep the node awake forever. Reset preflightSleepDeferrals to 0 whenever
     * sleepOnNextExecution is armed.
     */
    bool shouldDeferDeepSleep()
    {
        if (doPreflightSleep(true) || preflightSleepDeferrals >= MAX_PREFLIGHT_SLEEP_DEFERRALS)
            return false;
        preflightSleepDeferrals++;
        LOG_DEBUG("Radio busy, defer deep sleep");
        return true;
    }

    // While sleepOnNextExecution is pending, counts how often the deep sleep was postponed
    // because doPreflightSleep() vetoed it (e.g. radio still transmitting)
    uint32_t preflightSleepDeferrals = 0;

    // Telemetry publish history
    // Note: lastSentToMesh is not here: it's tracked externally
    uint32_t lastSentToPhone = 0;
    uint32_t lastSentToMqtt = 0;
    uint32_t lastRead = 0;

    /// Highest Biscuit tier this node will encode at, capped by what was compiled in. A member
    /// rather than a constant so the tiers can be exercised at runtime in one build, which is
    /// how the suite covers all four without four binaries; a node could also cap it to trade
    /// compression for decode cost on the receiving side.
    uint8_t biscuitMaxTier = BISCUIT_MAX_TIER;
    virtual meshtastic_MeshPacket *allocTelemetryHistoryPacket() { return nullptr; }
    virtual meshtastic_MeshPacket *allocTelemetryPacket(const meshtastic_Telemetry &m) { return nullptr; }

    virtual void onPublishedTelemetry(const meshtastic_MeshPacket &p) {}

    bool publishTelemetry(const meshtastic_Telemetry &m, NodeNum dest, bool phoneOnly);
    template <typename T, uint8_t N> bool publishBufferedTelemetry(TelemetryHistoryBuffer<T, N> &history, PublishTarget target);
};
