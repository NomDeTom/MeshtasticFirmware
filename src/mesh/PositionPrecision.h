#pragma once

#include "meshtastic/channel.pb.h"
#include "meshtastic/mesh.pb.h"
#include <stdint.h>

uint32_t getPositionPrecisionForChannel(const meshtastic_Channel &channel);
uint32_t getPositionPrecisionForChannel(uint8_t channelIndex);
void applyPositionPrecision(meshtastic_Position &position, uint32_t precision);
bool applyPositionPrecision(meshtastic_MeshPacket &packet, uint32_t precision);
bool applyPositionPrecisionForChannel(meshtastic_MeshPacket &packet, uint8_t channelIndex);

/// Clamp a relayed POSITION_APP payload to at most maxPrecisionBits, rewriting
/// the payload in place. precision_bits == 0 with coordinates present is
/// treated as full precision (32) — the classic doxing case. Never increases
/// precision: a payload already at or below the ceiling is left untouched, as
/// is one without coordinates. `clamped` (optional) reports whether the
/// payload was rewritten. Returns false only on payload decode failure.
bool clampRelayedPositionPrecision(meshtastic_MeshPacket &packet, uint32_t maxPrecisionBits, bool *clamped = nullptr);
