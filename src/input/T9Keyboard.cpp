#include "T9Keyboard.h"
#include "main.h"

#ifdef HAS_T9_KEYBOARD

T9Keyboard *t9Keyboard = nullptr;

T9Keyboard::T9Keyboard() : concurrency::OSThread("T9Keyboard") {}

T9Keyboard::~T9Keyboard()
{
    disable();
}

void T9Keyboard::init()
{
    // Validate pin configuration
#if !defined(T9_ROW_PIN_0) || !defined(T9_COL_PIN_0)
#error "T9 keyboard enabled but T9_ROW_PIN_0 or T9_COL_PIN_0 not defined in variant.h"
#endif

#ifdef T9_COL_PIN_4
    hasJoystickCol = true;
#endif

    LOG_DEBUG("T9Keyboard: Initializing GPIO matrix (joystick col: %s)", hasJoystickCol ? "yes" : "no");
    initPins();
    inputBroker->registerSource(this);
    LOG_INFO("T9Keyboard: Initialized");
}

void T9Keyboard::initPins()
{
    // Initialize row pins as outputs (defined in variant.h)
#ifdef T9_ROW_PIN_0
    rowPins.push_back(T9_ROW_PIN_0);
    pinMode(T9_ROW_PIN_0, OUTPUT);
    digitalWrite(T9_ROW_PIN_0, HIGH);
#endif
#ifdef T9_ROW_PIN_1
    rowPins.push_back(T9_ROW_PIN_1);
    pinMode(T9_ROW_PIN_1, OUTPUT);
    digitalWrite(T9_ROW_PIN_1, HIGH);
#endif
#ifdef T9_ROW_PIN_2
    rowPins.push_back(T9_ROW_PIN_2);
    pinMode(T9_ROW_PIN_2, OUTPUT);
    digitalWrite(T9_ROW_PIN_2, HIGH);
#endif

    // Initialize column pins as inputs with pull-ups
#ifdef T9_COL_PIN_0
    colPins.push_back(T9_COL_PIN_0);
    pinMode(T9_COL_PIN_0, INPUT_PULLUP);
#endif
#ifdef T9_COL_PIN_1
    colPins.push_back(T9_COL_PIN_1);
    pinMode(T9_COL_PIN_1, INPUT_PULLUP);
#endif
#ifdef T9_COL_PIN_2
    colPins.push_back(T9_COL_PIN_2);
    pinMode(T9_COL_PIN_2, INPUT_PULLUP);
#endif
#ifdef T9_COL_PIN_3
    colPins.push_back(T9_COL_PIN_3);
    pinMode(T9_COL_PIN_3, INPUT_PULLUP);
#endif

    // Joystick column (directional controls)
#ifdef T9_COL_PIN_4
    colPins.push_back(T9_COL_PIN_4);
    pinMode(T9_COL_PIN_4, INPUT_PULLUP);
    LOG_DEBUG("T9Keyboard: Joystick column enabled on pin %d", T9_COL_PIN_4);
#endif

    LOG_DEBUG("T9Keyboard: Configured %d rows, %d cols", (int)rowPins.size(), (int)colPins.size());
}

int32_t T9Keyboard::runOnce()
{
    uint32_t now = millis();
    if (now - lastScan < SCAN_INTERVAL) {
        return SCAN_INTERVAL;
    }

    lastScan = now;
    scanMatrix();
    checkMultiTapTimeout();
    return SCAN_INTERVAL;
}

void T9Keyboard::scanMatrix()
{
    // Scan each row
    for (uint8_t row = 0; row < rowPins.size(); row++) {
        // Pull row low, all others high
        for (uint8_t r = 0; r < rowPins.size(); r++) {
            digitalWrite(rowPins[r], (r == row) ? LOW : HIGH);
        }

        // Small delay for settle
        delayMicroseconds(500);

        // Read regular letter key columns (0-3)
        for (uint8_t col = 0; col < COLS; col++) {
            if (col >= colPins.size()) break;
            uint8_t pressed = !digitalRead(colPins[col]); // Pull-up: active LOW
            if (pressed != keymatrix[row][col]) {
                keymatrix[row][col] = pressed;
                if (pressed) {
                    handleKeyPress(row, col);
                }
            }
        }

        // Read joystick column if enabled (column 4)
        if (hasJoystickCol && JOYSTICK_COL < colPins.size()) {
            static uint8_t joystickMatrix[ROWS] = {};
            uint8_t pressed = !digitalRead(colPins[JOYSTICK_COL]); // Pull-up: active LOW
            if (pressed != joystickMatrix[row]) {
                joystickMatrix[row] = pressed;
                if (pressed) {
                    handleJoystickPress(row);
                }
            }
        }
    }

    // Return all rows to inactive state
    for (uint8_t r = 0; r < rowPins.size(); r++) {
        digitalWrite(rowPins[r], HIGH);
    }
}

void T9Keyboard::handleKeyPress(uint8_t row, uint8_t col)
{
    if (row >= rowPins.size() || col >= colPins.size()) {
        return;
    }

    int keyIndex = getKeyIndex(row, col);
    uint32_t now = millis();
    uint32_t timeSinceLastTap = now - lastTapTime;

    // If same key pressed again within timeout, increment tap count
    if (keyIndex == lastKeyIndex && timeSinceLastTap < MULTI_TAP_TIMEOUT) {
        tapCount = (tapCount + 1) % T9_CHAR_COUNTS[row][col];
        lastTapTime = now;
        LOG_DEBUG("T9Keyboard: Key %d,%d tap #%d", row, col, tapCount + 1);
    }
    // Different key or timeout - emit last selection and start new
    else {
        if (lastKeyIndex >= 0) {
            emitCurrentSelection();
        }
        lastKeyIndex = keyIndex;
        tapCount = 0;
        lastTapTime = now;
        LOG_DEBUG("T9Keyboard: Key %d,%d first tap (index %d)", row, col, keyIndex);
    }
}

void T9Keyboard::checkMultiTapTimeout()
{
    if (lastKeyIndex < 0) {
        return;
    }

    uint32_t now = millis();
    if (now - lastTapTime >= MULTI_TAP_TIMEOUT) {
        emitCurrentSelection();
        lastKeyIndex = -1;
        tapCount = 0;
    }
}

void T9Keyboard::emitCurrentSelection()
{
    if (lastKeyIndex < 0) {
        return;
    }

    uint8_t row = lastKeyIndex / COLS;
    uint8_t col = lastKeyIndex % COLS;

    char selectedChar = getCharAtIndex(lastKeyIndex, tapCount);
    if (selectedChar != '\0') {
        InputEvent event = createCharEvent(selectedChar);
        notifyObservers(&event);
        LOG_DEBUG("T9Keyboard: Emitting '%c' from key %d,%d (tap %d)", selectedChar, row, col, tapCount);
    }
}

char T9Keyboard::getCharAtIndex(int keyIndex, uint8_t tapIdx) const
{
    if (keyIndex < 0 || keyIndex >= NUM_KEYS) {
        return '\0';
    }

    uint8_t row = keyIndex / COLS;
    uint8_t col = keyIndex % COLS;

    if (row >= ROWS || col >= COLS) {
        return '\0';
    }

    if (tapIdx >= T9_CHAR_COUNTS[row][col]) {
        return '\0';
    }

    const char *chars = T9_CHARS[row][col];
    if (chars == nullptr) {
        return '\0';
    }

    return chars[tapIdx];
}

InputEvent T9Keyboard::createCharEvent(char c)
{
    InputEvent event = {};
    event.source = "T9Keyboard";
    event.inputEvent = INPUT_BROKER_MATRIXKEY;
    event.kbchar = c;
    return event;
}

InputEvent T9Keyboard::createNavigationEvent(input_broker_event evt)
{
    InputEvent event = {};
    event.source = "T9Keyboard";
    event.inputEvent = evt;
    event.kbchar = 0;
    return event;
}

void T9Keyboard::handleJoystickPress(uint8_t row)
{
    input_broker_event evt = mapJoystickRow(row);
    if (evt != INPUT_BROKER_NONE) {
        InputEvent event = createNavigationEvent(evt);
        notifyObservers(&event);
        LOG_DEBUG("T9Keyboard: Joystick row %d -> event 0x%02x", row, evt);
    }
}

input_broker_event T9Keyboard::mapJoystickRow(uint8_t row) const
{
    // Map joystick rows to navigation events based on variant.h defines
    // User defines which row = which direction
#ifdef T9_JOYSTICK_ROW_UP
    if (row == T9_JOYSTICK_ROW_UP) return INPUT_BROKER_UP;
#endif
#ifdef T9_JOYSTICK_ROW_DOWN
    if (row == T9_JOYSTICK_ROW_DOWN) return INPUT_BROKER_DOWN;
#endif
#ifdef T9_JOYSTICK_ROW_LEFT
    if (row == T9_JOYSTICK_ROW_LEFT) return INPUT_BROKER_LEFT;
#endif
#ifdef T9_JOYSTICK_ROW_RIGHT
    if (row == T9_JOYSTICK_ROW_RIGHT) return INPUT_BROKER_RIGHT;
#endif
#ifdef T9_JOYSTICK_ROW_CLICK
    if (row == T9_JOYSTICK_ROW_CLICK) return INPUT_BROKER_SELECT;
#endif

    return INPUT_BROKER_NONE;
}

#endif // HAS_T9_KEYBOARD
