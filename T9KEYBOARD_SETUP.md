# T9 Phone Keyboard Integration

## Overview

GPIO-based 3×4 matrix keyboard driver with standard phone layout (0-9, \*, #) and multi-tap
text entry, plus an optional 5th matrix column wired as a joystick d-pad.

## Files

- `src/input/T9Keyboard.h` / `src/input/T9Keyboard.cpp` - Driver
- `src/input/T9Keyboard_variant_template.h` - Configuration template

## Integration Steps

### 1. Define GPIO pins in your variant.h

Add the following to your device's `variants/<arch>/<device>/variant.h`
(all three row pins and four column pins are required):

```c
#define HAS_T9_KEYBOARD 1

// Row pins (outputs, driven LOW one at a time while scanning)
#define T9_ROW_PIN_0 8
#define T9_ROW_PIN_1 9
#define T9_ROW_PIN_2 10

// Column pins (inputs with pull-up, read LOW when a key is pressed)
#define T9_COL_PIN_0 16
#define T9_COL_PIN_1 17
#define T9_COL_PIN_2 18
#define T9_COL_PIN_3 19

// Joystick column (optional)
#define T9_COL_PIN_4 20
```

**Replace pin numbers with your actual GPIO assignments.**

### 2. Hardware connections

**Row pins (outputs):** rows idle HIGH and are driven LOW one at a time while scanning.

**Letter key columns 0-3 (inputs with pull-up):** when the key at (row, col) is pressed,
that column reads LOW while its row is active.

**Joystick column 4 (optional):** all joystick directions share column 4; each
direction/click is wired to a different row.

### 3. Joystick configuration

If using column 4, tell the driver which row is which direction:

```cpp
#define T9_JOYSTICK_ROW_UP 0    // Up press -> row 0
#define T9_JOYSTICK_ROW_DOWN 1  // Down press -> row 1
#define T9_JOYSTICK_ROW_LEFT 2  // Left press -> row 2
#define T9_JOYSTICK_ROW_RIGHT 2
#define T9_JOYSTICK_ROW_CLICK 1
```

If the layout is unknown, press each direction and watch the debug logs to see which
row activates, then fill in the defines. Unmapped rows are ignored.

### 4. Key layout

```
Letter keys (columns 0-3):        Joystick (column 4):

1  2  3  4    <- row 0            one direction per row,
5  6  7  8    <- row 1            per the T9_JOYSTICK_ROW_*
9  *  0  #    <- row 2            defines above
```

Multi-tap character mapping (`T9_CHARS` in `T9Keyboard.cpp`):

```cpp
static const char *const T9_CHARS[3][4] = {{"1", "2abc", "3def", "4ghi"},
                                           {"5jkl", "6mno", "7pqrs", "8tuv"},
                                           {"9wxyz", "*", "0 ", "#"}};
```

### 5. Build & flash

```bash
pio run -e <your_variant>
pio run -e <your_variant> -t upload
```

## How it works

1. **Matrix scanning** - a background thread scans every 10 ms: each row is pulled LOW
   in turn and all columns are read. A column going LOW means the key at (row, col)
   was pressed.
2. **Multi-tap** - pressing the same key repeatedly within 1.5 s cycles through its
   characters (first tap is the digit's first letter, e.g. `2` = `2`→`a`→`b`→`c`).
   Waiting 1.5 s or pressing a different key locks in the selection, which is emitted
   as an `INPUT_BROKER_MATRIXKEY` event with `kbchar` set.
3. **Shift / caps lock** - `*` is shift:
   - Single press: one-shot shift — the next letter is emitted as a capital.
   - Double press (within 1.5 s): toggles caps lock. The on-screen keyboard shows
     `CAP` while caps lock is active.
   - Digits and symbols are unaffected by shift.
4. **Joystick** - mapped rows emit `INPUT_BROKER_UP/DOWN/LEFT/RIGHT/SELECT`
   navigation events.
5. **InputBroker** - all events flow to the UI/menus via the existing InputBroker
   system.

Example, typing "Hello":

1. Press `*` once → shift on (one-shot)
2. Press `4` twice, wait → emits `H`
3. Press `3` twice, wait → emits `e`
4. Press `5` three times, wait → emits `l` (twice for both l's)
5. Press `6` three times, wait → emits `o`

## Customization

- **Key mapping**: edit the `T9_CHARS` array in `T9Keyboard.cpp`.
- **Scan rate / multi-tap timeout**: change `SCAN_INTERVAL` / `MULTI_TAP_TIMEOUT` in
  `T9Keyboard.h`.
