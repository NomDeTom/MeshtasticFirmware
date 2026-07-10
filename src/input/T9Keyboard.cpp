#include "T9Keyboard.h"
#include "configuration.h"
#include <string.h>

#ifdef HAS_T9_KEYBOARD

#if !defined(T9_ROW_PIN_0) || !defined(T9_ROW_PIN_1) || !defined(T9_ROW_PIN_2) || !defined(T9_COL_PIN_0) ||                      \
    !defined(T9_COL_PIN_1) || !defined(T9_COL_PIN_2) || !defined(T9_COL_PIN_3)
#error "HAS_T9_KEYBOARD requires T9_ROW_PIN_0..2 and T9_COL_PIN_0..3 in variant.h"
#endif

T9Keyboard *t9Keyboard = nullptr;

// Rows are driven LOW one at a time; columns are inputs with pull-ups (active
// LOW).
static const uint8_t rowPins[] = {T9_ROW_PIN_0, T9_ROW_PIN_1, T9_ROW_PIN_2};
static const uint8_t colPins[] = {
    T9_COL_PIN_0, T9_COL_PIN_1, T9_COL_PIN_2, T9_COL_PIN_3,
#ifdef T9_COL_PIN_4
    T9_COL_PIN_4, // Optional joystick column: each row is a d-pad direction
#endif
};
static constexpr uint8_t NUM_COLS_SCANNED = sizeof(colPins);

// T9 multi-tap keymap: press a key repeatedly to cycle through its characters.
// Standard phone layout:
// 1        2(abc)   3(def)   4(ghi)
// 5(jkl)   6(mno)   7(pqrs)  8(tuv)
// 9(wxyz)  *(SHIFT) 0(space) #(#)
static const char *const T9_CHARS[3][4] = {
    {"1", "2abc", "3def", "4ghi"}, {"5jkl", "6mno", "7pqrs", "8tuv"}, {"9wxyz", "*", "0 ", "#"}};

T9Keyboard::T9Keyboard() : concurrency::OSThread("T9Keyboard") {}

void T9Keyboard::init()
{
    for (uint8_t i = 0; i < ROWS; i++) {
        pinMode(rowPins[i], OUTPUT);
        digitalWrite(rowPins[i], HIGH);
    }
    for (uint8_t i = 0; i < NUM_COLS_SCANNED; i++)
        pinMode(colPins[i], INPUT_PULLUP);

    inputBroker->registerSource(this);
    LOG_INFO("T9Keyboard: %u rows, %u cols", ROWS, NUM_COLS_SCANNED);
}

int32_t T9Keyboard::runOnce()
{
    scanMatrix();
    checkMultiTapTimeout();
    return SCAN_INTERVAL;
}

void T9Keyboard::scanMatrix()
{
    for (uint8_t row = 0; row < ROWS; row++) {
        // Pull this row low, all others high
        for (uint8_t r = 0; r < ROWS; r++)
            digitalWrite(rowPins[r], (r == row) ? LOW : HIGH);
        delayMicroseconds(10); // settle time for the pull-ups

        for (uint8_t col = 0; col < NUM_COLS_SCANNED; col++) {
            uint8_t pressed = !digitalRead(colPins[col]); // Pull-up: active LOW
            if (pressed == keymatrix[row][col])
                continue;
            keymatrix[row][col] = pressed;
            if (!pressed)
                continue;
            if (col < COLS)
                handleKeyPress(row, col);
            else
                handleJoystickPress(row);
        }
    }

    // Return all rows to inactive state
    for (uint8_t r = 0; r < ROWS; r++)
        digitalWrite(rowPins[r], HIGH);
}

void T9Keyboard::handleKeyPress(uint8_t row, uint8_t col)
{
    uint32_t now = millis();

    // The asterisk key (row 2, col 1) is shift; a double-press toggles caps lock
    if (row == 2 && col == 1) {
        emitCurrentSelection(); // lock in any pending letter first
        if (shiftActive && now - lastShiftPressTime < MULTI_TAP_TIMEOUT) {
            capsLockActive = !capsLockActive;
            shiftActive = false;
        } else {
            shiftActive = true;
            lastShiftPressTime = now;
        }
        return;
    }

    int8_t keyIndex = (int8_t)(row * COLS + col);
    if (keyIndex == lastKeyIndex && now - lastTapTime < MULTI_TAP_TIMEOUT) {
        // Same key again within the timeout: cycle to its next character
        tapCount = (tapCount + 1) % strlen(T9_CHARS[row][col]);
    } else {
        emitCurrentSelection(); // different key: lock in the previous selection
        lastKeyIndex = keyIndex;
        tapCount = 0;
    }
    lastTapTime = now;
}

void T9Keyboard::checkMultiTapTimeout()
{
    if (lastKeyIndex >= 0 && millis() - lastTapTime >= MULTI_TAP_TIMEOUT)
        emitCurrentSelection();
}

void T9Keyboard::emitCurrentSelection()
{
    if (lastKeyIndex < 0)
        return;

    char c = T9_CHARS[lastKeyIndex / COLS][lastKeyIndex % COLS][tapCount];
    if ((shiftActive || capsLockActive) && c >= 'a' && c <= 'z')
        c -= 'a' - 'A';
    shiftActive = false; // one-shot
    emit(INPUT_BROKER_MATRIXKEY, c);

    lastKeyIndex = -1;
    tapCount = 0;
}

void T9Keyboard::emit(input_broker_event evt, char c)
{
    InputEvent e = {};
    e.source = "T9Keyboard";
    e.inputEvent = evt;
    e.kbchar = c;
    notifyObservers(&e);
}

void T9Keyboard::handleJoystickPress(uint8_t row)
{
    // Which row maps to which direction is defined in variant.h
    (void)row; // unused if no T9_JOYSTICK_ROW_* rows are mapped
    input_broker_event evt = INPUT_BROKER_NONE;
#ifdef T9_JOYSTICK_ROW_UP
    if (row == T9_JOYSTICK_ROW_UP)
        evt = INPUT_BROKER_UP;
#endif
#ifdef T9_JOYSTICK_ROW_DOWN
    if (row == T9_JOYSTICK_ROW_DOWN)
        evt = INPUT_BROKER_DOWN;
#endif
#ifdef T9_JOYSTICK_ROW_LEFT
    if (row == T9_JOYSTICK_ROW_LEFT)
        evt = INPUT_BROKER_LEFT;
#endif
#ifdef T9_JOYSTICK_ROW_RIGHT
    if (row == T9_JOYSTICK_ROW_RIGHT)
        evt = INPUT_BROKER_RIGHT;
#endif
#ifdef T9_JOYSTICK_ROW_CLICK
    if (row == T9_JOYSTICK_ROW_CLICK)
        evt = INPUT_BROKER_SELECT;
#endif
    if (evt != INPUT_BROKER_NONE)
        emit(evt, 0);
}

#endif // HAS_T9_KEYBOARD
