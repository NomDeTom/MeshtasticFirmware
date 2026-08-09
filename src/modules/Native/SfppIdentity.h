#pragma once

#include <cstddef>
#include <cstdint>

class SfppIdentity
{
  public:
    static constexpr size_t ObjectIdSize = 32;
    static constexpr size_t ShortIdSize = 4;
    static constexpr size_t ChecksumContributionSize = 8;
    static constexpr size_t ScopeIdSize = 16;

    static bool calculateObjectId(uint8_t objectId[ObjectIdSize], uint32_t from, uint32_t to, uint32_t packetId,
                                  const uint8_t *encryptedPayload, size_t encryptedPayloadSize);
    static bool calculateShortId(uint8_t shortId[ShortIdSize], const uint8_t objectId[ObjectIdSize]);
    static bool calculateChecksumContribution(uint8_t checksumContribution[ChecksumContributionSize],
                                              const uint8_t objectId[ObjectIdSize]);
    static bool calculateScopeId(uint8_t scopeId[ScopeIdSize], const uint8_t *channelPsk, size_t channelPskSize);
};
