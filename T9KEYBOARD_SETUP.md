# T9 Phone Keyboard Integration

## Overview
GPIO-based 3×4 matrix keyboard driver supporting standard phone layout (0-9, *, #) plus joystick button.

## Files Created
- `src/input/T9Keyboard.h` - Header
- `src/input/T9Keyboard.cpp` - Implementation
- `src/input/T9Keyboard_variant_template.h` - Configuration template

## Integration Steps

### 1. Define GPIO pins in your variant.h

Add the following to your device's `variants/<arch>/<device>/variant.h`:

```c
#define HAS_T9_KEYBOARD 1

// Row pins (outputs, active LOW)
#define T9_ROW_PIN_0 8
#define T9_ROW_PIN_1 9
#define T9_ROW_PIN_2 10

// Column pins (inputs with pull-up, sense LOW when key pressed)
#define T9_COL_PIN_0 16
#define T9_COL_PIN_1 17
#define T9_COL_PIN_2 18
#define T9_COL_PIN_3 19

// Joystick button (optional)
#define T9_JOYSTICK_PIN 20
```

**Replace pin numbers with your actual GPIO assignments.**

### 2. Hardware connections

**Row pins (outputs):**
- Connect each row pin to one side of the diodes in that row
- Rows are pulled HIGH (inactive), driven LOW to activate

**Letter key columns 0-3 (inputs with pull-up):**
- Connect each column pin to one side of the pull-up resistors
- When a key at (row, col) is pressed, the corresponding column reads LOW

**Joystick column 4 (optional, input with pull-up):**
- Joystick directions (up, down, left, right, click) share the same column
- Each direction/click is wired to a different row in column 4
- When joystick pressed in a direction, that row's column 4 pin reads LOW

### 3. Joystick Configuration

**If using joystick column 4:**

The joystick directions are wired to different rows in column 4. You need to identify which row triggers which direction:

1. Physically test each joystick direction
2. Check which row gets pulled low for each direction/click
3. Add these #defines to your variant.h:

```cpp
#define T9_JOYSTICK_ROW_UP 0        // Up press -> row 0
#define T9_JOYSTICK_ROW_DOWN 1      // Down press -> row 1
#define T9_JOYSTICK_ROW_LEFT 2      // Left press -> row 2
#define T9_JOYSTICK_ROW_RIGHT 2     // Right press -> row 2 (if same as left, up/down distinguishes)
#define T9_JOYSTICK_ROW_CLICK 1     // Click -> row 1
```

Map these **before** you know the layout:
- Press joystick up → watch logs for which row gets activated
- Add that row number to `T9_JOYSTICK_ROW_UP`
- Repeat for each direction

### 4. Key Layout

```
Letter keys (columns 0-3):

1  2  3  4     COL0 COL1 COL2 COL3
5  6  7  8  → ROW0, ROW1, ROW2
9  *  0  #

Joystick (column 4):
UP       → COL4, ROW0 (if T9_JOYSTICK_ROW_UP = 0)
DOWN     → COL4, ROW1 (if T9_JOYSTICK_ROW_DOWN = 1)
LEFT/RIGHT/CLICK → COL4, ROW2 (if mapped to T9_JOYSTICK_ROW_* = 2)
```

T9 character mapping in `T9Keyboard.cpp`:
```cpp
static constexpr const char *T9_CHARS[ROWS][COLS] = {
    {"1", "2abc", "3def", "4ghi"},
    {"5jkl", "6mno", "7pqrs", "8tuv"},
    {"9wxyz", "*+", "0 ", "#"}
};
```

### 4. Build & Test

```bash
# Build for your variant
pio run -e <your_variant>

# Flash
pio run -e <your_variant> -t upload
```

## How It Works

1. **Matrix scanning** - Background thread scans at ~10ms intervals
   - Pull each row LOW (active), read all columns
   - Detect key press (column goes LOW)
   - Release key when column returns HIGH

2. **Event generation** - Each key press emits an `InputEvent`:
   - Keyboard characters ('0'-'9', '*', '#') → sent as `kbchar`
   - Joystick button → sent as `INPUT_BROKER_SELECT` navigation event

3. **InputBroker** - Events flow to UI/menus via the existing InputBroker system

## Debugging

Enable verbose logging by adding to `src/DebugConfiguration.h`:

```cpp
#define InputBrokerDebug 1
```

Watch serial output for:
```
T9Keyboard: Initialized
T9Keyboard: Key 0,0 pressed = '1' (0x31)
T9Keyboard: Joystick pressed
```

## Multi-tap (T9 Text Input)

**Built into driver** - press key repeatedly to cycle through characters:

- `2` = `a` → `b` → `c` → `2` (number)
- `3` = `d` → `e` → `f` → `3`
- `4` = `g` → `h` → `i` → `4`
- `5` = `j` → `k` → `l` → `5`
- `6` = `m` → `n` → `o` → `6`
- `7` = `p` → `q` → `r` → `s` → `7`
- `8` = `t` → `u` → `v` → `8`
- `9` = `w` → `x` → `y` → `z` → `9`
- `0` = space
- `*` = **SHIFT toggle** (capitals)
- `#` = `#`
- `1` = `1` (only one option, plus numeric)

**Shift/Caps (Capital Letters):**
- Press `*` key = toggle SHIFT/CAPS mode (no character emitted)
- When shift is active, all letter keys emit CAPITALS (A-Z)
- Shift state persists until toggled off by pressing `*` again
- Numbers and symbols unaffected by shift

**Timing:**
- Press same key multiple times within 1.5s to cycle through its letters
- Wait 1.5s or press different key to lock in selection and emit character
- Character sent via `INPUT_BROKER_MATRIXKEY` event with `kbchar` set to selected letter

Example: To type "Hello"
1. Press `*` → toggle shift ON (no char emitted)
2. Press `4` once → emit `H` (capital, shift is on)
3. Press `*` → toggle shift OFF
4. Press `3` twice → emit `e`
5. Press `5` three times → emit `l`
6. Press `5` three times → emit `l`
7. Press `6` three times → emit `o`

**Joystick:**
- `UP/DOWN/LEFT/RIGHT` = navigation (menu up/down/left/right)
- `CLICK` = SELECT (confirm menu selection)

## Customization

### Different key layout
Edit the `T9_KEYMAP` array in `T9Keyboard.cpp` to remap keys.

### Different scan rate
Change `SCAN_INTERVAL` in `T9Keyboard.h` (default 10ms).

### Debouncing
Add a press-state counter before calling `handleKeyChange()` if contacts are bouncy.

## Testing without hardware

During development, you can inject keyboard events via the `/serial` or BLE debug interface. See `InputBroker::injectInputEvent()`.

---