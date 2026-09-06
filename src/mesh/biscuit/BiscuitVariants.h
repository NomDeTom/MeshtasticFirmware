#pragma once

#include "mesh/generated/meshtastic/telemetry.pb.h"
#include <pb.h>

// Every metrics type a Biscuit batch can carry, with its Telemetry oneof tag. The tag is
// Telemetry's rather than TelemetryRecord's because the record oneof carries only three of
// the eight, and any of them must be batchable.
#define BISCUIT_METRICS_TYPES(X)                                                                                                 \
    X(meshtastic_DeviceMetrics, device_metrics)                                                                                  \
    X(meshtastic_EnvironmentMetrics, environment_metrics)                                                                        \
    X(meshtastic_AirQualityMetrics, air_quality_metrics)                                                                         \
    X(meshtastic_PowerMetrics, power_metrics)                                                                                    \
    X(meshtastic_LocalStats, local_stats)                                                                                        \
    X(meshtastic_HealthMetrics, health_metrics)                                                                                  \
    X(meshtastic_HostMetrics, host_metrics)                                                                                      \
    X(meshtastic_TrafficManagementStats, traffic_management_stats)

// The nanopb descriptor and variant tag for each, so Biscuit encodes any of them with no
// per-type encoder.
template <typename T> const pb_msgdesc_t *metricsDescriptor();
template <typename T> constexpr uint8_t variantTagFor();

#define BISCUIT_METRICS_TRAITS(TYPE, FIELD)                                                                                      \
    template <> inline const pb_msgdesc_t *metricsDescriptor<TYPE>()                                                             \
    {                                                                                                                            \
        return &TYPE##_msg;                                                                                                      \
    }                                                                                                                            \
    template <> constexpr uint8_t variantTagFor<TYPE>()                                                                          \
    {                                                                                                                            \
        return meshtastic_Telemetry_##FIELD##_tag;                                                                               \
    }
BISCUIT_METRICS_TYPES(BISCUIT_METRICS_TRAITS)
#undef BISCUIT_METRICS_TRAITS
