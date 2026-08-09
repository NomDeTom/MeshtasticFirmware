#pragma once

#include <cstddef>
#include <cstdint>

class StoreForwardArchiveObjectId
{
  public:
    static constexpr size_t ScopeIdSize = 16;
    static constexpr size_t ObjectIdSize = 32;

    static bool calculate(uint8_t objectId[ObjectIdSize], const uint8_t scopeId[ScopeIdSize], uint32_t from, uint32_t to,
                          uint32_t packetId, const uint8_t *encryptedPayload, size_t encryptedPayloadSize);
};
