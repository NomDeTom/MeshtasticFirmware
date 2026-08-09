#include "mesh/RadioInterface.h"
#include "mesh/generated/meshtastic/mesh.pb.h"

#include <cstdint>
#include <cstring>
#include <pb_encode.h>
#include <unity.h>

namespace
{
size_t encodeEnvelope(const meshtastic_StoreForwardPlusPlus &envelope, uint8_t *buffer, size_t bufferSize)
{
    pb_ostream_t stream = pb_ostream_from_buffer(buffer, bufferSize);
    TEST_ASSERT_TRUE(pb_encode(&stream, &meshtastic_StoreForwardPlusPlus_msg, &envelope));
    return stream.bytes_written;
}

meshtastic_StoreForwardPlusPlus makeMaximumBinding()
{
    meshtastic_StoreForwardPlusPlus envelope = meshtastic_StoreForwardPlusPlus_init_zero;
    envelope.sfpp_message_type = meshtastic_StoreForwardPlusPlus_SFPP_message_type_ARCHIVE_ENVELOPE;
    envelope.archive_protocol_version = 1;
    envelope.which_archive_message = meshtastic_StoreForwardPlusPlus_peer_binding_tag;

    auto &binding = envelope.archive_message.peer_binding;
    binding.sidecar_node_num = UINT32_MAX;
    binding.binding_generation = UINT32_MAX;
    binding.has_issued_at = true;
    binding.issued_at = UINT32_MAX;
    binding.has_expires_at = true;
    binding.expires_at = UINT32_MAX;
    memset(binding.sidecar_public_key, 0xff, sizeof(binding.sidecar_public_key));
    memset(binding.plane_id, 0xff, sizeof(binding.plane_id));
    memset(binding.scope_id, 0xff, sizeof(binding.scope_id));
    memset(binding.radio_profile_digest, 0xff, sizeof(binding.radio_profile_digest));
    return envelope;
}
} // namespace

void setUp() {}

void tearDown() {}

void testSfppBindingFitsSignedFrame(void)
{
    meshtastic_StoreForwardPlusPlus envelope = makeMaximumBinding();
    uint8_t payload[meshtastic_Constants_DATA_PAYLOAD_LEN] = {};
    const size_t payloadSize = encodeEnvelope(envelope, payload, sizeof(payload));

    meshtastic_Data data = meshtastic_Data_init_zero;
    data.portnum = meshtastic_PortNum_STORE_FORWARD_PLUSPLUS_APP;
    data.payload.size = payloadSize;
    memcpy(data.payload.bytes, payload, payloadSize);
    data.xeddsa_signature.size = sizeof(data.xeddsa_signature.bytes);

    size_t encodedDataSize = 0;
    TEST_ASSERT_TRUE(pb_get_encoded_size(&encodedDataSize, &meshtastic_Data_msg, &data));
    TEST_ASSERT_LESS_OR_EQUAL(MAX_LORA_PAYLOAD_LEN, encodedDataSize + MESHTASTIC_HEADER_LENGTH);
}

void testSfppCatchUpRequestCarriesEightScopes(void)
{
    meshtastic_StoreForwardPlusPlus envelope = meshtastic_StoreForwardPlusPlus_init_zero;
    envelope.sfpp_message_type = meshtastic_StoreForwardPlusPlus_SFPP_message_type_ARCHIVE_ENVELOPE;
    envelope.archive_protocol_version = 1;
    envelope.which_archive_message = meshtastic_StoreForwardPlusPlus_catch_up_request_tag;

    auto &request = envelope.archive_message.catch_up_request;
    request.scope_ids_count = 8;
    request.wait_for_slot = true;
    request.binding_generation = UINT32_MAX;
    memset(request.request_id, 0xff, sizeof(request.request_id));
    memset(request.scope_ids, 0xff, sizeof(request.scope_ids));

    uint8_t payload[meshtastic_Constants_DATA_PAYLOAD_LEN] = {};
    const size_t payloadSize = encodeEnvelope(envelope, payload, sizeof(payload));
    TEST_ASSERT_EQUAL(8, request.scope_ids_count);
    TEST_ASSERT_LESS_OR_EQUAL(meshtastic_Constants_DATA_PAYLOAD_LEN, payloadSize);
}

int main(int argc, char **argv)
{
    UNITY_BEGIN();
    RUN_TEST(testSfppBindingFitsSignedFrame);
    RUN_TEST(testSfppCatchUpRequestCarriesEightScopes);
    return UNITY_END();
}
