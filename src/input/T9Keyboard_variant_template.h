// T9 Keyboard GPIO configuration template
// Add these #defines to your variant.h to enable the T9 keyboard driver

#ifndef T9_KEYBOARD_TEMPLATE_H
#define T9_KEYBOARD_TEMPLATE_H

// ===== Enable T9 Keyboard =====
#define HAS_T9_KEYBOARD 1

// ===== GPIO Pin Configuration =====
// All three rows and columns 0-3 are required.
// Rows: driven as outputs (pulled LOW one at a time while scanning)
#define T9_ROW_PIN_0 8  // Row 0 (keys 1,2,3,4)
#define T9_ROW_PIN_1 9  // Row 1 (keys 5,6,7,8)
#define T9_ROW_PIN_2 10 // Row 2 (keys 9,*,0,#)

// Letter key columns: sense inputs with pull-ups (LOW when key pressed)
#define T9_COL_PIN_0 16 // Column 0 (1,5,9)
#define T9_COL_PIN_1 17 // Column 1 (2,6,*)
#define T9_COL_PIN_2 18 // Column 2 (3,7,0)
#define T9_COL_PIN_3 19 // Column 3 (4,8,#)

// Joystick column (optional) - directional pad, one direction per row
#define T9_COL_PIN_4 20

// Joystick row mappings - which row has which direction?
// Fill these in once you know the physical layout (unmapped rows are ignored)
// #define T9_JOYSTICK_ROW_UP 0
// #define T9_JOYSTICK_ROW_DOWN 1
// #define T9_JOYSTICK_ROW_LEFT 2
// #define T9_JOYSTICK_ROW_RIGHT 2
// #define T9_JOYSTICK_ROW_CLICK 0

// Key layout (physical):
// Row 0:  1  2  3  4
// Row 1:  5  6  7  8
// Row 2:  9  *  0  #

#endif // T9_KEYBOARD_TEMPLATE_H
