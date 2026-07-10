#pragma once

#include "InputBroker.h"
#include "Observer.h"
#include "concurrency/OSThread.h"
#include <vector>

/**
 * @brief T9 (phone) keyboard driver for 3x4 GPIO matrix
 * Supports standard phone layout (0-9, *, #) plus joystick button
 */
class T9Keyboard : public Observable<const InputEvent *>, private concurrency::OSThread
{
  public:
    T9Keyboard();
    ~T9Keyboard();
    void init();

  protected:
    int32_t runOnce() override;

  private:
    static constexpr uint8_t ROWS = 3;
    static constexpr uint8_t COLS = 4; // Letter keys (0-9, *, #)
    static constexpr uint8_t NUM_KEYS = ROWS * COLS;
    static constexpr uint8_t JOYSTICK_COL = 4; // Column index for joystick (if enabled)
    static constexpr uint32_t SCAN_INTERVAL = 10; // ms between scans
    static constexpr uint32_t MULTI_TAP_TIMEOUT = 1500; // ms to lock in multi-tap selection

    // Row and column GPIO pins (define in variant.h)
    std::vector<uint8_t> rowPins;
    std::vector<uint8_t> colPins;

    // Joystick column (optional) - same matrix columns but interpreted as directional
    bool hasJoystickCol = false;

    // Joystick row mappings (which row = which direction)
    // Define in variant.h if using joystick column
    // T9_JOYSTICK_ROW_UP, T9_JOYSTICK_ROW_DOWN, T9_JOYSTICK_ROW_LEFT,
    // T9_JOYSTICK_ROW_RIGHT, T9_JOYSTICK_ROW_CLICK

    // Key state tracking
    uint8_t keymatrix[ROWS][COLS] = {};
    uint8_t prevmatrix[ROWS][COLS] = {};
    uint32_t lastScan = 0;

    // Multi-tap state
    int lastKeyIndex = -1;
    uint8_t tapCount = 0;
    uint32_t lastTapTime = 0;

    // T9 keymap: multi-tap mappings
    // Each key has multiple characters, press repeatedly to cycle
    // Standard phone layout:
    // 1        2(abc)   3(def)   4(ghi)
    // 5(jkl)   6(mno)   7(pqrs)  8(tuv)
    // 9(wxyz)  *(+)     0(space) #(#)
    static constexpr const char *T9_CHARS[ROWS][COLS] = {
        {"1", "2abc", "3def", "4ghi"},
        {"5jkl", "6mno", "7pqrs", "8tuv"},
        {"9wxyz", "*+", "0 ", "#"}};

    static constexpr uint8_t T9_CHAR_COUNTS[ROWS][COLS] = {
        {1, 4, 4, 4},
        {4, 4, 5, 4},
        {5, 2, 2, 1}};

    void scanMatrix();
    void handleKeyPress(uint8_t row, uint8_t col);
    void handleJoystickPress(uint8_t row);
    void checkMultiTapTimeout();
    void emitCurrentSelection();
    void initPins();

    // Input event helpers
    InputEvent createCharEvent(char c);
    InputEvent createNavigationEvent(input_broker_event evt);

    // Multi-tap helpers
    int getKeyIndex(uint8_t row, uint8_t col) const { return row * COLS + col; }
    char getCharAtIndex(int keyIndex, uint8_t tapIdx) const;

    // Joystick helpers
    input_broker_event mapJoystickRow(uint8_t row) const;
};

extern T9Keyboard *t9Keyboard;
