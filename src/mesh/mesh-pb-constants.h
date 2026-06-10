#pragma once
#include <vector>

#include "mesh/generated/meshtastic/admin.pb.h"
#include "mesh/generated/meshtastic/deviceonly.pb.h"
#include "mesh/generated/meshtastic/localonly.pb.h"
#include "mesh/generated/meshtastic/mesh.pb.h"

// this file defines constants which come from mesh.options

// Tricky macro to let you find the sizeof a type member
#define member_size(type, member) sizeof(((type *)0)->member)

/// max number of packets which can be waiting for delivery to android - note, this value comes from mesh.options protobuf
// FIXME - max_count is actually 32 but we save/load this as one long string of preencoded MeshPacket bytes - not a big array in
// RAM #define MAX_RX_TOPHONE (member_size(DeviceState, receive_queue) / member_size(DeviceState, receive_queue[0]))
#ifndef MAX_RX_TOPHONE
#if defined(ARCH_ESP32) && !(defined(CONFIG_IDF_TARGET_ESP32C3) || defined(CONFIG_IDF_TARGET_ESP32S3))
#define MAX_RX_TOPHONE 8
#else
#define MAX_RX_TOPHONE 32
#endif
#endif

/// max number of QueueStatus packets which can be waiting for delivery to phone
#ifndef MAX_RX_QUEUESTATUS_TOPHONE
#define MAX_RX_QUEUESTATUS_TOPHONE 2
#endif

/// max number of MqttClientProxyMessage packets which can be waiting for delivery to phone
#ifndef MAX_RX_MQTTPROXY_TOPHONE
#define MAX_RX_MQTTPROXY_TOPHONE 8
#endif

/// max number of ClientNotification packets which can be waiting for delivery to phone
#ifndef MAX_RX_NOTIFICATION_TOPHONE
#define MAX_RX_NOTIFICATION_TOPHONE 2
#endif

/// Tighten this when the slim header shrinks; loosen only with deliberate
/// awareness of MAX_NUM_NODES impact per platform.
static_assert(sizeof(meshtastic_NodeInfoLite) <= 130, "NodeInfoLite size increased. Reconsider impact on MAX_NUM_NODES.");

// Compile satellite NodeDBs out on STM32WL (and the status DB also follows
// MESHTASTIC_EXCLUDE_STATUS).
#ifndef MESHTASTIC_EXCLUDE_POSITIONDB
#if defined(ARCH_STM32WL)
#define MESHTASTIC_EXCLUDE_POSITIONDB 1
#else
#define MESHTASTIC_EXCLUDE_POSITIONDB 0
#endif
#endif

#ifndef MESHTASTIC_EXCLUDE_TELEMETRYDB
#if defined(ARCH_STM32WL)
#define MESHTASTIC_EXCLUDE_TELEMETRYDB 1
#else
#define MESHTASTIC_EXCLUDE_TELEMETRYDB 0
#endif
#endif

#ifndef MESHTASTIC_EXCLUDE_ENVIRONMENTDB
#if defined(ARCH_STM32WL)
#define MESHTASTIC_EXCLUDE_ENVIRONMENTDB 1
#else
#define MESHTASTIC_EXCLUDE_ENVIRONMENTDB 0
#endif
#endif

#ifndef MESHTASTIC_EXCLUDE_STATUSDB
#if defined(ARCH_STM32WL) || defined(MESHTASTIC_EXCLUDE_STATUS)
#define MESHTASTIC_EXCLUDE_STATUSDB 1
#else
#define MESHTASTIC_EXCLUDE_STATUSDB 0
#endif
#endif

/// max number of nodes allowed in the nodeDB (the full-NodeInfoLite "hot +
/// short-tail" store; see .notes/nodedb-3tier-sizing.md). Long-tail identity
/// retention for evicted nodes is handled by the warm tier (WARM_NODE_COUNT).
#ifndef MAX_NUM_NODES
#if defined(ARCH_STM32WL)
#define MAX_NUM_NODES 10
#else
#define MAX_NUM_NODES 80
#endif
#endif

/// Cap on each satellite map (position/telemetry/environment/status). Only the
/// MAX_SATELLITE_NODES most-recently-heard nodes keep satellite payloads; the
/// rest of the hot store carries just the 96 B NodeInfoLite header. This is
/// what bounds both heap (the maps are ~408 B/node worst case) and the
/// nodes.proto file size.
#ifndef MAX_SATELLITE_NODES
#define MAX_SATELLITE_NODES (MAX_NUM_NODES / 2)
#endif

/// Warm tier: number of 40 B {num, last_heard, public_key} records retained
/// for nodes evicted from the hot store, primarily so DMs to/from them keep
/// decrypting. 0 disables the tier entirely.
/// nRF52840: sized to fill the two 16 KB raw-flash copies reclaimed from the
/// app region after LTO (see WarmNodeStore.h); (16384 - 16) / 40 = 409.
#ifndef WARM_NODE_COUNT
#if defined(ARCH_STM32WL)
#define WARM_NODE_COUNT 0
#elif defined(NRF52840_XXAA)
#define WARM_NODE_COUNT 400
#elif defined(ARCH_NRF52)
#define WARM_NODE_COUNT 128 // non-840 nRF52: file-backed in the 28 KB LittleFS, keep small
#elif defined(CONFIG_IDF_TARGET_ESP32S3)
#define WARM_NODE_COUNT 2000 // PSRAM-backed when available
#else
#define WARM_NODE_COUNT 320
#endif
#endif

/// Max number of channels allowed
#define MAX_NUM_CHANNELS (member_size(meshtastic_ChannelFile, channels) / member_size(meshtastic_ChannelFile, channels[0]))

// Traffic Management module configuration
// Enable per-variant by defining HAS_TRAFFIC_MANAGEMENT=1 in variant.h
#ifndef HAS_TRAFFIC_MANAGEMENT
#define HAS_TRAFFIC_MANAGEMENT 1
#endif

// HopScalingModule - variable hop module: dynamically adjusts broadcast hop_limit based on mesh density
// Enable per-variant by defining HAS_VARIABLE_HOPS=1 in variant.h
#ifdef ARCH_STM32WL
#define HAS_VARIABLE_HOPS 0
#endif
#ifndef HAS_VARIABLE_HOPS
#define HAS_VARIABLE_HOPS 1
#endif

// Cache size for traffic management (number of nodes to track)
// Can be overridden per-variant based on available memory
#ifndef TRAFFIC_MANAGEMENT_CACHE_SIZE
#if HAS_TRAFFIC_MANAGEMENT
#define TRAFFIC_MANAGEMENT_CACHE_SIZE 1000
#else
#define TRAFFIC_MANAGEMENT_CACHE_SIZE 0
#endif
#endif

/// helper function for encoding a record as a protobuf, any failures to encode are fatal and we will panic
/// returns the encoded packet size
size_t pb_encode_to_bytes(uint8_t *destbuf, size_t destbufsize, const pb_msgdesc_t *fields, const void *src_struct);

/// helper function for decoding a record as a protobuf, we will return false if the decoding failed
bool pb_decode_from_bytes(const uint8_t *srcbuf, size_t srcbufsize, const pb_msgdesc_t *fields, void *dest_struct);

/// Read from an Arduino File
bool readcb(pb_istream_t *stream, uint8_t *buf, size_t count);

/// Write to an arduino file
bool writecb(pb_ostream_t *stream, const uint8_t *buf, size_t count);

/** is_in_repeated is a macro/function that returns true if a specified word appears in a repeated protobuf array.
 * It relies on the following naming conventions from nanopb:
 *
 * pb_size_t ignore_incoming_count;
 * uint32_t ignore_incoming[3];
 */
bool is_in_helper(uint32_t n, const uint32_t *array, pb_size_t count);

#define is_in_repeated(name, n) is_in_helper(n, name, name##_count)
