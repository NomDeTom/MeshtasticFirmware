#include "SfppIdentity.h"

#include "Channels.h"
#include <SHA256.h>
#include <cstring>
#include <limits>

namespace
{
constexpr char ObjectIdDomainSeparator[] = "meshtastic-sfpp-object-v1";
constexpr char ChecksumDomainSeparator[] = "sfpp-ck-v3";
constexpr char ScopeIdDomainSeparator[] = "meshtastic-sfpp-scope-v1";

void updateUint16LittleEndian(SHA256 &hash, uint16_t value)
{
    const uint8_t encoded[] = {
        static_cast<uint8_t>(value),
        static_cast<uint8_t>(value >> 8),
    };
    hash.update(encoded, sizeof(encoded));
}

void updateUint32LittleEndian(SHA256 &hash, uint32_t value)
{
    const uint8_t encoded[] = {
        static_cast<uint8_t>(value),
        static_cast<uint8_t>(value >> 8),
        static_cast<uint8_t>(value >> 16),
        static_cast<uint8_t>(value >> 24),
    };
    hash.update(encoded, sizeof(encoded));
}

bool isPrivateScopeKey(const uint8_t *channelPsk, size_t channelPskSize)
{
    if (channelPsk == nullptr || (channelPskSize != 16 && channelPskSize != 32)) {
        return false;
    }

    CryptoKey key = {};
    memcpy(key.bytes, channelPsk, channelPskSize);
    key.length = static_cast<int8_t>(channelPskSize);
    return !cryptoKeyIsPublic(key) &&
           !(channelPskSize == sizeof(eventpsk) && memcmp(channelPsk, eventpsk, sizeof(eventpsk)) == 0);
}
} // namespace

bool SfppIdentity::calculateObjectId(uint8_t objectId[ObjectIdSize], uint32_t from, uint32_t to, uint32_t packetId,
                                     const uint8_t *encryptedPayload, size_t encryptedPayloadSize)
{
    if (objectId == nullptr || (encryptedPayloadSize != 0 && encryptedPayload == nullptr) ||
        encryptedPayloadSize > std::numeric_limits<uint16_t>::max()) {
        return false;
    }

    SHA256 hash;
    hash.reset();
    hash.update(ObjectIdDomainSeparator, sizeof(ObjectIdDomainSeparator));
    updateUint32LittleEndian(hash, from);
    updateUint32LittleEndian(hash, to);
    updateUint32LittleEndian(hash, packetId);
    updateUint16LittleEndian(hash, static_cast<uint16_t>(encryptedPayloadSize));
    if (encryptedPayloadSize != 0) {
        hash.update(encryptedPayload, encryptedPayloadSize);
    }
    hash.finalize(objectId, ObjectIdSize);
    return true;
}

bool SfppIdentity::calculateShortId(uint8_t shortId[ShortIdSize], const uint8_t objectId[ObjectIdSize])
{
    if (shortId == nullptr || objectId == nullptr) {
        return false;
    }

    uint8_t hash[ObjectIdSize] = {};
    SHA256 hasher;
    hasher.reset();
    hasher.update(objectId, ObjectIdSize);
    hasher.finalize(hash, sizeof(hash));
    memcpy(shortId, hash, ShortIdSize);
    return true;
}

bool SfppIdentity::calculateChecksumContribution(uint8_t checksumContribution[ChecksumContributionSize],
                                                 const uint8_t objectId[ObjectIdSize])
{
    if (checksumContribution == nullptr || objectId == nullptr) {
        return false;
    }

    uint8_t hash[ObjectIdSize] = {};
    SHA256 hasher;
    hasher.reset();
    hasher.update(ChecksumDomainSeparator, sizeof(ChecksumDomainSeparator) - 1);
    hasher.update(objectId, ObjectIdSize);
    hasher.finalize(hash, sizeof(hash));
    memcpy(checksumContribution, hash, ChecksumContributionSize);
    return true;
}

bool SfppIdentity::calculateScopeId(uint8_t scopeId[ScopeIdSize], const uint8_t *channelPsk, size_t channelPskSize)
{
    if (scopeId == nullptr || !isPrivateScopeKey(channelPsk, channelPskSize)) {
        return false;
    }

    uint8_t hash[ObjectIdSize] = {};
    SHA256 hasher;
    hasher.resetHMAC(channelPsk, channelPskSize);
    hasher.update(ScopeIdDomainSeparator, sizeof(ScopeIdDomainSeparator));
    hasher.finalizeHMAC(channelPsk, channelPskSize, hash, sizeof(hash));
    memcpy(scopeId, hash, ScopeIdSize);
    return true;
}
