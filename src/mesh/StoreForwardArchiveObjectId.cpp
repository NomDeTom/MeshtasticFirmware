#include "StoreForwardArchiveObjectId.h"

#include <SHA256.h>
#include <limits>

namespace
{
constexpr char ObjectIdDomainSeparator[] = "meshtastic-storeforward-archive-object-v1";

void updateUint32BigEndian(SHA256 &hash, uint32_t value)
{
    const uint8_t encoded[] = {
        static_cast<uint8_t>(value >> 24),
        static_cast<uint8_t>(value >> 16),
        static_cast<uint8_t>(value >> 8),
        static_cast<uint8_t>(value),
    };
    hash.update(encoded, sizeof(encoded));
}
} // namespace

bool StoreForwardArchiveObjectId::calculate(uint8_t objectId[ObjectIdSize], const uint8_t scopeId[ScopeIdSize], uint32_t from,
                                            uint32_t to, uint32_t packetId, const uint8_t *encryptedPayload,
                                            size_t encryptedPayloadSize)
{
    if (objectId == nullptr || scopeId == nullptr || (encryptedPayloadSize != 0 && encryptedPayload == nullptr) ||
        encryptedPayloadSize > std::numeric_limits<uint32_t>::max()) {
        return false;
    }

    SHA256 hash;
    hash.reset();
    // Keep object IDs independent of host endianness and include the trailing NUL.
    hash.update(ObjectIdDomainSeparator, sizeof(ObjectIdDomainSeparator));
    hash.update(scopeId, ScopeIdSize);
    updateUint32BigEndian(hash, from);
    updateUint32BigEndian(hash, to);
    updateUint32BigEndian(hash, packetId);
    updateUint32BigEndian(hash, static_cast<uint32_t>(encryptedPayloadSize));
    if (encryptedPayloadSize != 0) {
        hash.update(encryptedPayload, encryptedPayloadSize);
    }
    hash.finalize(objectId, ObjectIdSize);
    return true;
}
