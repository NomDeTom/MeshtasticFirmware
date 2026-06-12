#include "Default.h"

#include "meshUtils.h"

// Convert seconds to ms, clamping at INT32_MAX (~24.86 days)
static inline uint32_t secondsToMsClamped(uint32_t secs)
{
    constexpr uint32_t MAX_MS = static_cast<uint32_t>(INT32_MAX);
    return (secs > MAX_MS / 1000U) ? MAX_MS : secs * 1000U;
}

uint32_t Default::getConfiguredOrDefaultMs(uint32_t configuredInterval, uint32_t defaultInterval)
{
    return secondsToMsClamped(configuredInterval > 0 ? configuredInterval : defaultInterval);
}

uint32_t Default::getConfiguredOrDefaultMs(uint32_t configuredInterval)
{
    return secondsToMsClamped(configuredInterval > 0 ? configuredInterval : default_broadcast_interval_secs);
}

uint32_t Default::getConfiguredOrDefault(uint32_t configured, uint32_t defaultValue)
{
    if (configured > 0)
        return configured;
    return defaultValue;
}
/**
 * Calculates the scaled value of the configured or default value in ms based on the number of online nodes.
 *
 * For example a default of 30 minutes (1800 seconds * 1000) would yield:
 *   45 nodes = 2475 * 1000
 *   60 nodes = 4500 * 1000
 *   75 nodes = 6525 * 1000
 *   90 nodes = 8550 * 1000
 * @param configured The configured value.
 * @param defaultValue The default value.
 * @param numOnlineNodes The number of online nodes.
 * @return The scaled value of the configured or default value.
 */
uint32_t Default::getConfiguredOrDefaultMsScaled(uint32_t configured, uint32_t defaultValue, uint32_t numOnlineNodes)
{
    // If we are a router, we don't scale the value. It's already significantly higher.
    if (config.device.role == meshtastic_Config_DeviceConfig_Role_ROUTER ||
        config.device.role == meshtastic_Config_DeviceConfig_Role_ROUTER_LATE)
        return getConfiguredOrDefaultMs(configured, defaultValue);

    // Additionally if we're a tracker or sensor, we want priority to send position and telemetry
    if (IS_ONE_OF(config.device.role, meshtastic_Config_DeviceConfig_Role_SENSOR, meshtastic_Config_DeviceConfig_Role_TRACKER,
                  meshtastic_Config_DeviceConfig_Role_TAK_TRACKER))
        return getConfiguredOrDefaultMs(configured, defaultValue);

    // Saturate at INT32_MAX to match secondsToMsClamped: float→uint32_t when
    // out of range is UB, and the result is consumed as an int32_t downstream.
    constexpr uint32_t MAX_MS = static_cast<uint32_t>(INT32_MAX);
    uint32_t base = getConfiguredOrDefaultMs(configured, defaultValue);
    float coef = congestionScalingCoefficient(numOnlineNodes);
    if (static_cast<double>(base) * static_cast<double>(coef) >= static_cast<double>(MAX_MS))
        return MAX_MS;
    return base * coef;
}

uint32_t Default::getConfiguredOrDefaultMsScaled(uint32_t configured, uint32_t defaultValue, uint32_t numOnlineNodes,
                                                 TrafficType type)
{
    uint32_t baseMs = getConfiguredOrDefaultMsScaled(configured, defaultValue, numOnlineNodes);

    if (!myRegion || !myRegion->profile)
        return baseMs;

    int8_t throttle =
        (type == TrafficType::POSITION) ? myRegion->profile->positionThrottle : myRegion->profile->telemetryThrottle;

    // throttle <= 0 means unset; 1 is the neutral multiplier — skip the multiply for performance
    if (throttle <= 1)
        return baseMs;

    constexpr uint32_t MAX_MS = static_cast<uint32_t>(INT32_MAX);
    uint64_t result = static_cast<uint64_t>(baseMs) * static_cast<uint64_t>(throttle);
    return result >= static_cast<uint64_t>(MAX_MS) ? MAX_MS : static_cast<uint32_t>(result);
}

uint32_t Default::getConfiguredOrMinimumValue(uint32_t configured, uint32_t minValue)
{
    // If zero, intervals should be coalesced later by getConfiguredOrDefault... methods
    if (configured == 0)
        return configured;

    return configured < minValue ? minValue : configured;
}

uint32_t Default::roleDefaultIntervalSecs(meshtastic_Config_DeviceConfig_Role role, TrafficType type)
{
    if (type == TrafficType::NODEINFO)
        return default_node_info_broadcast_secs; // same cadence for every role
    // Position and telemetry share the same role defaults
    if (role == meshtastic_Config_DeviceConfig_Role_ROUTER || role == meshtastic_Config_DeviceConfig_Role_ROUTER_LATE)
        return ONE_DAY / 2;
    return 60 * 60;
}

uint32_t Default::roleScaledIntervalMs(meshtastic_Config_DeviceConfig_Role role, TrafficType type, uint32_t numOnlineNodes)
{
    constexpr uint32_t MAX_MS = static_cast<uint32_t>(INT32_MAX);
    uint32_t baseMs = secondsToMsClamped(roleDefaultIntervalSecs(role, type));

    // Same exemptions getConfiguredOrDefaultMsScaled applies to our own role:
    // routers already run long intervals, reporting roles get priority.
    const bool exempt = IS_ONE_OF(role, meshtastic_Config_DeviceConfig_Role_ROUTER,
                                  meshtastic_Config_DeviceConfig_Role_ROUTER_LATE, meshtastic_Config_DeviceConfig_Role_SENSOR,
                                  meshtastic_Config_DeviceConfig_Role_TRACKER, meshtastic_Config_DeviceConfig_Role_TAK_TRACKER);
    if (!exempt) {
        float coef = congestionScalingCoefficient(numOnlineNodes);
        if (static_cast<double>(baseMs) * static_cast<double>(coef) >= static_cast<double>(MAX_MS))
            return MAX_MS;
        baseMs = static_cast<uint32_t>(baseMs * coef);
    }

    if (myRegion && myRegion->profile && type != TrafficType::NODEINFO) {
        int8_t throttle =
            (type == TrafficType::POSITION) ? myRegion->profile->positionThrottle : myRegion->profile->telemetryThrottle;
        if (throttle > 1) {
            uint64_t result = static_cast<uint64_t>(baseMs) * static_cast<uint64_t>(throttle);
            baseMs = result >= static_cast<uint64_t>(MAX_MS) ? MAX_MS : static_cast<uint32_t>(result);
        }
    }
    return baseMs;
}

uint8_t Default::roleRateAllowance(meshtastic_Config_DeviceConfig_Role role, uint8_t clientBaseline)
{
    switch (role) {
    case meshtastic_Config_DeviceConfig_Role_ROUTER:
    case meshtastic_Config_DeviceConfig_Role_ROUTER_LATE: {
        // Router routine budget is ~0.5/h; scale proportionally with the
        // configured client baseline (2 at the default of 5).
        uint8_t scaled = clientBaseline * 2 / 5;
        return scaled < 2 ? 2 : scaled;
    }
    case meshtastic_Config_DeviceConfig_Role_TRACKER:
    case meshtastic_Config_DeviceConfig_Role_TAK_TRACKER:
    case meshtastic_Config_DeviceConfig_Role_SENSOR:
        // Reporting roles may legitimately run at the 5-minute smart-broadcast
        // minimum; never grant them less than the compiled tracker allowance.
        return clientBaseline > default_traffic_mgmt_rate_limit_max_packets_tracker
                   ? clientBaseline
                   : default_traffic_mgmt_rate_limit_max_packets_tracker;
    case meshtastic_Config_DeviceConfig_Role_REPEATER:
    case meshtastic_Config_DeviceConfig_Role_ROUTER_CLIENT:
        return 0; // deprecated roles: routine broadcasts are dropped
    default:
        return clientBaseline;
    }
}

uint8_t Default::getConfiguredOrDefaultHopLimit(uint8_t configured)
{
#if USERPREFS_EVENT_MODE
    return (configured > HOP_RELIABLE) ? HOP_RELIABLE : config.lora.hop_limit;
#else
    return (configured >= HOP_MAX) ? HOP_MAX : config.lora.hop_limit;
#endif
}
