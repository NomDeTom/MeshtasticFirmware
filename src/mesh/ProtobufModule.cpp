#include "ProtobufModule.h"
#include "configuration.h"

// One body for every T. The nine instantiations of the old template bodies differed only in
// sizeof(T) - the frame size, the memset length and the scratch offset - so the type comes in as a
// size and the decode is shared. noinline/noclone: sizeof(T) is a constant at each call site, so
// IPA-CP would otherwise clone this back into nine specialised copies under -flto.
__attribute__((noinline, noclone)) ProtobufModuleBase::ScratchDecode
ProtobufModuleBase::decodeScratch(const meshtastic_MeshPacket &mp, void *scratch, size_t size, bool logReceived)
{
    if (mp.which_payload_variant != meshtastic_MeshPacket_decoded_tag || mp.decoded.portnum != ourPortNum)
        return ScratchDecode::NotOurs;

    const meshtastic_Data &p = mp.decoded;
    memset(scratch, 0, size);
    if (!pb_decode_from_bytes(p.payload.bytes, p.payload.size, fields, scratch)) {
        LOG_ERROR("Error decoding proto module");
        return ScratchDecode::Failed;
    }
    if (logReceived)
        LOG_INFO("Received %s from=0x%08x, id=0x%08x, portnum=%d, payloadlen=%d", name, mp.from, mp.id, p.portnum,
                 p.payload.size);
    return ScratchDecode::Decoded;
}
