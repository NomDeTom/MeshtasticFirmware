#pragma once

#include "InputBroker.h"
#include "Observer.h"
#include "concurrency/OSThread.h"

/**
 * @brief T9 (phone) keyboard driver for a 3x4 GPIO matrix with multi-tap
 * character entry, plus an optional 5th column wired as a joystick d-pad.
 *
 * Pins are defined in variant.h — see T9Keyboard_variant_template.h.
 */
class T9Keyboard : public Observable<const InputEvent *>, private concurrency::OSThread
{
  public:
    T9Keyboard();
    void init();

    bool isCapsLockActive() const { return capsLockActive; }
    bool isShiftActive() const { return shiftActive; }

  protected:
    int32_t runOnce() override;

  private:
    static constexpr uint8_t ROWS = 3;
    static constexpr uint8_t COLS = 4;                  // Letter keys (0-9, *, #)
    static constexpr uint32_t SCAN_INTERVAL = 10;       // ms between scans
    static constexpr uint32_t MULTI_TAP_TIMEOUT = 1500; // ms to lock in multi-tap selection

    // Debounced key state, letter columns plus the optional joystick column
    uint8_t keymatrix[ROWS][COLS + 1] = {};

    // Multi-tap state
    int8_t lastKeyIndex = -1; // row * COLS + col of the key being cycled, -1 if none
    uint8_t tapCount = 0;
    uint32_t lastTapTime = 0;
    bool shiftActive = false;    // One-shot shift (clears after typing a character)
    bool capsLockActive = false; // Persistent caps lock (double-press shift)
    uint32_t lastShiftPressTime = 0;

    void scanMatrix();
    void handleKeyPress(uint8_t row, uint8_t col);
    void handleJoystickPress(uint8_t row);
    void checkMultiTapTimeout();
    void emitCurrentSelection();
    void emit(input_broker_event evt, char c);
};

extern T9Keyboard *t9Keyboard;
