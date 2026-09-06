#pragma once
#include "../mesh/generated/meshtastic/telemetry.pb.h"
#include "BaseTelemetryModule.h"
#include "NodeDB.h"
#include "ProtobufModule.h"
#include "TelemetryHistory.h"
#include <OLEDDisplay.h>
#include <OLEDDisplayUi.h>

// Readings held for batched publishing; a board with RAM to spare can raise it in variant.h.
#ifndef DEVICE_TELEMETRY_HISTORY_SIZE
#define DEVICE_TELEMETRY_HISTORY_SIZE 16
#endif

class DeviceTelemetryModule : private concurrency::OSThread,
                              public BaseTelemetryModule,
                              public ProtobufModule<meshtastic_Telemetry>
{
    CallbackObserver<DeviceTelemetryModule, const meshtastic::Status *> nodeStatusObserver =
        CallbackObserver<DeviceTelemetryModule, const meshtastic::Status *>(this, &DeviceTelemetryModule::handleStatusUpdate);

  public:
    DeviceTelemetryModule()
        : concurrency::OSThread("DeviceTelemetry"),
          ProtobufModule("DeviceTelemetry", meshtastic_PortNum_TELEMETRY_APP, &meshtastic_Telemetry_msg)
    {
        nodeStatusObserver.observe(&nodeStatus->onNewStatus);
        setIntervalFromNow(setStartDelay()); // Wait until NodeInfo is sent
    }
    virtual bool wantUIFrame() { return false; }

  protected:
    /** Called to handle a particular incoming message
    @return true if you've guaranteed you've handled this message and no other handlers should be considered for it
    */
    virtual bool handleReceivedProtobuf(const meshtastic_MeshPacket &mp, meshtastic_Telemetry *p) override;
    virtual meshtastic_MeshPacket *allocReply() override;
    virtual int32_t runOnce() override;
    /**
     * Send our Telemetry into the mesh
     */
    bool sendTelemetry(NodeNum dest = NODENUM_BROADCAST, bool phoneOnly = false);

  private:
    meshtastic_Telemetry getDeviceTelemetry();
    meshtastic_Telemetry getLocalStatsTelemetry();

    void sendLocalStatsToPhone();
    uint32_t sendToPhoneIntervalMs = SECONDS_IN_MINUTE * 1000;           // Send to phone every minute
    uint32_t sendStatsToPhoneIntervalMs = 15 * SECONDS_IN_MINUTE * 1000; // Send stats to phone every 15 minutes
    uint32_t lastSentStatsToPhone = 0;

  protected:
    // Telemetry record history, shared by the mesh and mqtt publish paths
    TelemetryHistoryBuffer<meshtastic_DeviceMetrics, DEVICE_TELEMETRY_HISTORY_SIZE> history;
};