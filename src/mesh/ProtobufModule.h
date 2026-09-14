#pragma once
#include "SinglePortModule.h"

/**
 * A base class for mesh modules that assume that they are sending/receiving one particular protobuf based
 * payload.  Using one particular app ID.
 *
 * If you are using protobufs to encode your packets (recommended) you can use this as a baseclass for your module
 * and avoid a bunch of boilerplate code.
 */
/**
 * The half of ProtobufModule<T> that does not depend on T. The decode differed only in sizeof(T),
 * so it is shared here rather than instantiated once per message type.
 */
class ProtobufModuleBase : protected SinglePortModule
{
  protected:
    const pb_msgdesc_t *fields;

    ProtobufModuleBase(const char *_name, meshtastic_PortNum _ourPortNum, const pb_msgdesc_t *_fields)
        : SinglePortModule(_name, _ourPortNum), fields(_fields)
    {
    }

    enum class ScratchDecode { NotOurs, Decoded, Failed };
    /// Decode into `scratch` when the packet is ours to read. Defined in ProtobufModule.cpp.
    ScratchDecode decodeScratch(const meshtastic_MeshPacket &mp, void *scratch, size_t size, bool logReceived);
};

template <class T> class ProtobufModule : protected ProtobufModuleBase
{
  public:
    uint16_t numOnlineNodes = 0;
    /** Constructor
     * name is for debugging output
     */
    ProtobufModule(const char *_name, meshtastic_PortNum _ourPortNum, const pb_msgdesc_t *_fields)
        : ProtobufModuleBase(_name, _ourPortNum, _fields)
    {
    }

  protected:
    /**
     * Handle a received message, the data field in the message is already decoded and is provided
     *
     * In general decoded will always be !NULL.  But in some special applications (where you have handling packets
     * for multiple port numbers, decoding will ONLY be attempted for packets where the portnum matches our expected ourPortNum.
     */
    virtual bool handleReceivedProtobuf(const meshtastic_MeshPacket &mp, T *decoded) = 0;

    /** Called to make changes to a particular incoming message
     */
    virtual void alterReceivedProtobuf(meshtastic_MeshPacket &mp, T *decoded){};

    /**
     * Return a mesh packet which has been preinited with a particular protobuf data payload and port number.
     * You can then send this packet (after customizing any of the payload fields you might need) with
     * service->sendToMesh()
     */
    meshtastic_MeshPacket *allocDataProtobuf(const T &payload)
    {
        // Update our local node info with our position (even if we don't decide to update anyone else)
        meshtastic_MeshPacket *p = allocDataPacket();
        if (!p)
            return nullptr;

        p->decoded.payload.size =
            pb_encode_to_bytes(p->decoded.payload.bytes, sizeof(p->decoded.payload.bytes), fields, &payload);
        // LOG_DEBUG("did encode");
        return p;
    }

    /**
     * Gets the short name from the sender of the mesh packet
     * Returns "???" if unknown sender
     */
    const char *getSenderShortName(const meshtastic_MeshPacket &mp)
    {
        auto node = nodeDB->getMeshNode(getFrom(&mp));
        const char *sender = (node && nodeInfoLiteHasUser(node) && node->short_name[0]) ? node->short_name : "???";
        return sender;
    }

    int handleStatusUpdate(const meshtastic::Status *arg)
    {
        if (arg->getStatusType() == STATUS_TYPE_NODE) {
            numOnlineNodes = nodeStatus->getNumOnline();
        }
        return 0;
    }

  private:
    /** Called to handle a particular incoming message

    @return ProcessMessage::STOP if you've guaranteed you've handled this message and no other handlers should be considered for
    it
    */
    virtual ProcessMessage handleReceived(const meshtastic_MeshPacket &mp) override
    {
        // FIXME - we currently update position data in the DB only if the message was a broadcast or destined to us
        // it would be better to update even if the message was destined to others.

        T scratch;
        T *decoded = NULL;
        switch (decodeScratch(mp, &scratch, sizeof(scratch), /*logReceived=*/true)) {
        case ScratchDecode::Failed:
            return ProcessMessage::STOP; // if we can't decode it, nobody can process it!
        case ScratchDecode::Decoded:
            decoded = &scratch;
            break;
        case ScratchDecode::NotOurs:
            break;
        }

        return handleReceivedProtobuf(mp, decoded) ? ProcessMessage::STOP : ProcessMessage::CONTINUE;
    }

    /** Called to alter a particular incoming message
     */
    virtual void alterReceived(meshtastic_MeshPacket &mp) override
    {
        T scratch;
        if (decodeScratch(mp, &scratch, sizeof(scratch), /*logReceived=*/false) == ScratchDecode::Decoded)
            alterReceivedProtobuf(mp, &scratch);
    }
};
