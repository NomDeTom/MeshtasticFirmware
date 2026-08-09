#include "modules/Native/SfppIdentity.h"

#include <cstdlib>
#include <cstring>
#include <limits>
#include <string>
#include <unity.h>

namespace
{
void hexToBytes(uint8_t *result, const std::string &hex)
{
    for (size_t i = 0; i < hex.size(); i += 2) {
        result[i / 2] = static_cast<uint8_t>(strtol(hex.substr(i, 2).c_str(), nullptr, 16));
    }
}
} // namespace

void setUp() {}

void tearDown() {}

void testSfppObjectIdentity(void)
{
    const uint8_t payload[] = {0xde, 0xad, 0xbe, 0xef, 0x00};
    uint8_t objectId[SfppIdentity::ObjectIdSize] = {};
    uint8_t expected[SfppIdentity::ObjectIdSize] = {};

    hexToBytes(expected, "bf1be28f016eeec942eab981b9fe49e315dec77abe9e2ac0c0761b7ba0855b78");
    TEST_ASSERT_TRUE(SfppIdentity::calculateObjectId(objectId, 0x01020304, 0xa0b0c0d0, 0x10203040, payload, sizeof(payload)));
    TEST_ASSERT_EQUAL_MEMORY(expected, objectId, sizeof(expected));

    hexToBytes(expected, "9eefae2bfa6b86cd8e7c3b6afe3380838747980404fa7b8a8c347599372a591c");
    TEST_ASSERT_TRUE(SfppIdentity::calculateObjectId(objectId, 0x01020304, 0xa0b0c0d0, 0x10203040, nullptr, 0));
    TEST_ASSERT_EQUAL_MEMORY(expected, objectId, sizeof(expected));
}

void testSfppShortIdAndChecksumContribution(void)
{
    uint8_t objectId[SfppIdentity::ObjectIdSize] = {};
    uint8_t shortId[SfppIdentity::ShortIdSize] = {};
    uint8_t checksum[SfppIdentity::ChecksumContributionSize] = {};
    uint8_t expectedShortId[SfppIdentity::ShortIdSize] = {};
    uint8_t expectedChecksum[SfppIdentity::ChecksumContributionSize] = {};

    hexToBytes(objectId, "bf1be28f016eeec942eab981b9fe49e315dec77abe9e2ac0c0761b7ba0855b78");
    hexToBytes(expectedShortId, "b6c1afa0");
    hexToBytes(expectedChecksum, "57aaaa5212c0ab05");

    TEST_ASSERT_TRUE(SfppIdentity::calculateShortId(shortId, objectId));
    TEST_ASSERT_EQUAL_MEMORY(expectedShortId, shortId, sizeof(expectedShortId));
    TEST_ASSERT_TRUE(SfppIdentity::calculateChecksumContribution(checksum, objectId));
    TEST_ASSERT_EQUAL_MEMORY(expectedChecksum, checksum, sizeof(expectedChecksum));
}

void testSfppScopeIdentity(void)
{
    const uint8_t privatePsk[] = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f};
    const uint8_t eventPsk[] = {0x38, 0x4b, 0xbc, 0xc0, 0x1d, 0xc0, 0x22, 0xd1, 0x81, 0xbf, 0x36, 0xb8, 0x61, 0x21, 0xe1, 0xfb,
                                0x96, 0xb7, 0x2e, 0x55, 0xbf, 0x74, 0x22, 0x7e, 0x9d, 0x6a, 0xfb, 0x48, 0xd6, 0x4c, 0xb1, 0xa1};
    uint8_t defaultPsk[16] = {0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59, 0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01};
    uint8_t scopeId[SfppIdentity::ScopeIdSize] = {};
    uint8_t expected[SfppIdentity::ScopeIdSize] = {};

    hexToBytes(expected, "5359d0b1b648c6df640cbc14be064220");
    TEST_ASSERT_TRUE(SfppIdentity::calculateScopeId(scopeId, privatePsk, sizeof(privatePsk)));
    TEST_ASSERT_EQUAL_MEMORY(expected, scopeId, sizeof(expected));

    TEST_ASSERT_FALSE(SfppIdentity::calculateScopeId(scopeId, nullptr, 0));
    TEST_ASSERT_FALSE(SfppIdentity::calculateScopeId(scopeId, privatePsk, 1));
    TEST_ASSERT_FALSE(SfppIdentity::calculateScopeId(scopeId, defaultPsk, sizeof(defaultPsk)));
    defaultPsk[sizeof(defaultPsk) - 1]++;
    TEST_ASSERT_FALSE(SfppIdentity::calculateScopeId(scopeId, defaultPsk, sizeof(defaultPsk)));
    TEST_ASSERT_FALSE(SfppIdentity::calculateScopeId(scopeId, eventPsk, sizeof(eventPsk)));
}

void testSfppIdentityRejectsInvalidInput(void)
{
    const uint8_t payload[] = {0x00};
    uint8_t objectId[SfppIdentity::ObjectIdSize] = {};
    uint8_t shortId[SfppIdentity::ShortIdSize] = {};
    uint8_t checksum[SfppIdentity::ChecksumContributionSize] = {};

    TEST_ASSERT_FALSE(SfppIdentity::calculateObjectId(nullptr, 1, 2, 3, payload, sizeof(payload)));
    TEST_ASSERT_FALSE(SfppIdentity::calculateObjectId(objectId, 1, 2, 3, nullptr, 1));
    TEST_ASSERT_FALSE(SfppIdentity::calculateShortId(nullptr, objectId));
    TEST_ASSERT_FALSE(SfppIdentity::calculateChecksumContribution(checksum, nullptr));

    if (std::numeric_limits<size_t>::max() > std::numeric_limits<uint16_t>::max()) {
        TEST_ASSERT_FALSE(SfppIdentity::calculateObjectId(objectId, 1, 2, 3, payload,
                                                          static_cast<size_t>(std::numeric_limits<uint16_t>::max()) + 1));
    }
}

int main(int argc, char **argv)
{
    UNITY_BEGIN();
    RUN_TEST(testSfppObjectIdentity);
    RUN_TEST(testSfppShortIdAndChecksumContribution);
    RUN_TEST(testSfppScopeIdentity);
    RUN_TEST(testSfppIdentityRejectsInvalidInput);
    return UNITY_END();
}
