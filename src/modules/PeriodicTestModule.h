#pragma once
#if !EXCLUDE_TEST_MODULE
#include "FSCommon.h"
#include "concurrency/OSThread.h"
#include <cstdint>

struct LR2021DcdcProfile {
    uint32_t rise;
    uint32_t fall;
    uint32_t freq;
    bool ldoOnly;
    const char *label;
};

class PeriodicTestModule : private concurrency::OSThread
{
  public:
    PeriodicTestModule();

    const LR2021DcdcProfile &getCurrentDcdcProfile() const;
    uint8_t getCurrentDcdcProfileIndex() const { return dcdcProfileIndex; }
    uint8_t getDcdcProfileCount() const;

  protected:
    int32_t runOnce() override;

  private:
    void advanceDcdcProfile();
    void saveToDisk() const;
    void loadFromDisk();

    bool firstFired = false;
    uint32_t lastTickMs = 0;
    uint8_t dcdcProfileIndex = 0;    // next boot's profile (persisted)
    uint8_t appliedProfileIndex = 0; // profile actually applied this boot (for logging)
};

extern PeriodicTestModule *periodicTestModule;
#endif // !EXCLUDE_TEST_MODULE