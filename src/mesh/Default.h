#pragma once
#include <MeshRadio.h>
#include <NodeDB.h>
#include <RadioInterface.h>
#include <cmath>
#include <cstdint>
#include <meshUtils.h>
#define ONE_DAY 24 * 60 * 60
#define ONE_MINUTE_MS 60 * 1000
#define THIRTY_SECONDS_MS 30 * 1000
#define TWO_SECONDS_MS 2 * 1000
#define FIVE_SECONDS_MS 5 * 1000
#define TEN_SECONDS_MS 10 * 1000
#define MAX_INTERVAL INT32_MAX // FIXME: INT32_MAX to avoid overflow issues with Apple clients but should be UINT32_MAX

#define min_default_telemetry_interval_secs IF_ROUTER(ONE_DAY / 2, 30 * 60)
#define default_gps_update_interval IF_ROUTER(ONE_DAY, 2 * 60)
#define default_telemetry_broadcast_interval_secs IF_ROUTER(ONE_DAY / 2, 60 * 60)
#define default_broadcast_interval_secs IF_ROUTER(ONE_DAY / 2, 60 * 60)
#define default_broadcast_smart_minimum_interval_secs 5 * 60
#define min_default_broadcast_interval_secs IF_ROUTER(ONE_DAY / 2, 60 * 60)
#define min_default_broadcast_smart_minimum_interval_secs 5 * 60
#define default_wait_bluetooth_secs IF_ROUTER(1, 60)
#define default_sds_secs IF_ROUTER(ONE_DAY, UINT32_MAX) // Default to forever super deep sleep
#define default_ls_secs IF_ROUTER(ONE_DAY, 5 * 60)
#define default_min_wake_secs 10
#define default_screen_on_secs IF_ROUTER(1, 60 * 10)
#define default_node_info_broadcast_secs 3 * 60 * 60
#define default_neighbor_info_broadcast_secs 6 * 60 * 60
#define min_node_info_broadcast_secs 60 * 60 // No regular broadcasts of more than once an hour
#define min_neighbor_info_broadcast_secs 4 * 60 * 60
#define default_map_publish_interval_secs 60 * 60

enum class TrafficType { POSITION, TELEMETRY, NODEINFO };

// Traffic management defaults
#define default_traffic_mgmt_position_precision_bits 19               // ~90m grid cells (±45m)
#define default_traffic_mgmt_position_min_interval_secs (ONE_DAY / 2) // 12 hours between identical positions
#define default_traffic_mgmt_rate_limit_window_secs (60 * 60)         // per-hour budget framing
#define default_traffic_mgmt_rate_limit_max_packets 5                 // CLIENT baseline: own ~2.5/h routine budget + 1-2
#define default_traffic_mgmt_rate_limit_max_packets_tracker 14        // 5-min smart-min positions + telemetry headroom
#define default_traffic_mgmt_hop_grace 1                              // extra hops for others beyond our recommendation
#define max_traffic_mgmt_hop_grace 2
#define default_traffic_mgmt_port_interval_permissiveness_8ths 4 // others' interval = sender-role interval x 4/8
#define default_traffic_mgmt_precision_clamp_bits 13             // what the default channel grants our own node
#define min_traffic_mgmt_precision_clamp_bits 10
#define default_traffic_mgmt_politeness_threshold_8ths 12 // (rate/allowed)x(hop_start/suggested) > 1.5 acts

// Hop scaling defaults
#define default_hop_scaling_min_target_nodes 40          // walk threshold: first hop reaching this cumulative count
#define default_hop_scaling_max_target_nodes 80          // generous extension ceiling (2 × min)
#define default_hop_scaling_min_target_nodes_floor 5     // minimum allowed min_target_nodes
#define default_hop_scaling_max_target_nodes_ceiling 512 // maximum allowed max_target_nodes

#ifdef USERPREFS_RINGTONE_NAG_SECS
#define default_ringtone_nag_secs USERPREFS_RINGTONE_NAG_SECS
#else
#define default_ringtone_nag_secs 15
#endif
#define default_network_ipv6_enabled false

#define default_mqtt_address "mqtt.meshtastic.org"
#define default_mqtt_username "meshdev"
#define default_mqtt_password "large4cats"
#define default_mqtt_root "msh"
#define default_mqtt_encryption_enabled true
#define default_mqtt_tls_enabled false

#define IF_ROUTER(routerVal, normalVal)                                                                                          \
    ((config.device.role == meshtastic_Config_DeviceConfig_Role_ROUTER ||                                                        \
      config.device.role == meshtastic_Config_DeviceConfig_Role_ROUTER_LATE)                                                     \
         ? (routerVal)                                                                                                           \
         : (normalVal))

class Default
{
  public:
    static uint32_t getConfiguredOrDefaultMs(uint32_t configuredInterval);
    static uint32_t getConfiguredOrDefaultMs(uint32_t configuredInterval, uint32_t defaultInterval);
    static uint32_t getConfiguredOrDefault(uint32_t configured, uint32_t defaultValue);
    // Note: numOnlineNodes uses uint32_t to match the public API and allow flexibility,
    // even though internal node counts use uint16_t (max 65535 nodes)
    static uint32_t getConfiguredOrDefaultMsScaled(uint32_t configured, uint32_t defaultValue, uint32_t numOnlineNodes);
    static uint32_t getConfiguredOrDefaultMsScaled(uint32_t configured, uint32_t defaultValue, uint32_t numOnlineNodes,
                                                   TrafficType type);
    static uint8_t getConfiguredOrDefaultHopLimit(uint8_t configured);
    static uint32_t getConfiguredOrMinimumValue(uint32_t configured, uint32_t minValue);

    // Default broadcast interval the firmware would impose on a node of the
    // given role (the IF_ROUTER values evaluated for an arbitrary role rather
    // than our own config.device.role). Used to judge OTHER nodes' traffic
    // against their role's baseline, never cross-role.
    static uint32_t roleDefaultIntervalSecs(meshtastic_Config_DeviceConfig_Role role, TrafficType type);

    // roleDefaultIntervalSecs in ms with the same congestion scaling and
    // regional throttling a well-behaved node of that role would apply to its
    // own broadcasts (roles exempt from congestion scaling stay exempt).
    static uint32_t roleScaledIntervalMs(meshtastic_Config_DeviceConfig_Role role, TrafficType type, uint32_t numOnlineNodes);

    // Routine-broadcast packets per rate window allowed from a sender of the
    // given role, derived from the CLIENT baseline (configured or default).
    // 0 means the role's routine broadcasts are dropped (deprecated roles).
    static uint8_t roleRateAllowance(meshtastic_Config_DeviceConfig_Role role, uint8_t clientBaseline);

  private:
    // Note: Kept as uint32_t to match the public API parameter type
    static float congestionScalingCoefficient(uint32_t numOnlineNodes)
    {
        if (numOnlineNodes <= 40) {
            return 1.0;
        } else {
            // Resolve SF and BW from preset or manual config
            // When use_preset is true, config.lora.spread_factor and bandwidth may be 0
            // because applyModemConfig() sets them on RadioInterface, not on config.lora
            float bwKHz;
            uint8_t sf;
            uint8_t cr;
            if (config.lora.use_preset) {
                modemPresetToParams(config.lora.modem_preset, false, bwKHz, sf, cr);
            } else {
                sf = config.lora.spread_factor;
                bwKHz = bwCodeToKHz(config.lora.bandwidth);
            }

            // Guard against invalid values
            sf = clampSpreadFactor(sf);
            bwKHz = clampBandwidthKHz(bwKHz);

            // throttlingFactor = 2^SF / (BW_in_kHz * scaling_divisor)
            // With scaling_divisor=100:
            // In SF11 and BW=250khz (longfast), this gives 0.08192 rather than the original 0.075
            // In SF10 and BW=250khz (mediumslow), this gives 0.04096 rather than the original 0.04
            // In SF9 and BW=250khz (mediumfast), this gives 0.02048 rather than the original 0.02
            // In SF7 and BW=250khz (shortfast), this gives 0.00512 rather than the original 0.01
            float throttlingFactor = static_cast<float>(pow_of_2(sf)) / (bwKHz * 100.0f);

#if USERPREFS_EVENT_MODE
            // If we are in event mode, scale down the throttling factor by 4
            throttlingFactor = static_cast<float>(pow_of_2(sf)) / (bwKHz * 25.0f);
#endif

            // Scaling up traffic based on number of nodes over 40
            int nodesOverForty = (numOnlineNodes - 40);
            return 1.0 + (nodesOverForty * throttlingFactor); // Each number of online node scales by throttle factor
        }
    }
};