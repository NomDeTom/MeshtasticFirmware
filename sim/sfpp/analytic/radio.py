#!/usr/bin/env python3
"""Airtime, twice over.

Two independent implementations of LoRa time on air:

  toa_datasheet - the SX1276/SX1268 section 6.1.4 formula in floating point, written
                  from the datasheet.
  toa_radiolib  - a transcription of RadioLib's integer arithmetic, which is what the
                  firmware actually calls through RadioInterface::getPacketTime ->
                  computePacketTime -> lora.calculateTimeOnAir.

They are kept separate on purpose. Agreement between them is evidence the airtime
numbers in the charts are not a private mistake; validate.py asserts it. Preset
parameters are the firmware's own, from modemPresetToParams in src/mesh/MeshRadio.h.
"""

import math

# sf, bandwidth Hz, coding rate denominator. RadioLib's DataRate_t documents codingRate
# as the denominator (5 for 4/5), and modemPresetToParams sets cr the same way, so this
# is 5 or 8 rather than a 1..4 index - getting that wrong understates airtime fivefold.
PRESETS = {
    "ShortFast": (7, 250000, 5),
    "MediumFast": (9, 250000, 5),
    "LongFast": (11, 250000, 5),
    "LongSlow": (12, 125000, 8),
}

MESHTASTIC_HEADER = 16  # PacketHeader, on air with every frame
PREAMBLE = 16  # preambleLengthDefault in RadioInterface.h
PAYLOAD_BUDGET = 233  # Data.payload max_size


def ldro_for(sf, bw):
    """Low data rate optimisation, on when a symbol lasts longer than 16 ms.

    Strictly greater, matching the reference calculator at
    nomdetom.github.io/lora-airtime-calculator.html. Nothing in the Meshtastic preset
    table lands exactly on 16 ms, so the boundary is academic - but the two should not
    disagree on a case either could hit.
    """
    return 1 if (2**sf) / bw > 0.016 else 0


def toa_datasheet(payload_bytes, preset):
    """Seconds for one frame. Explicit header, CRC on, from the datasheet formula."""
    sf, bw, cr = PRESETS[preset]
    total = payload_bytes + MESHTASTIC_HEADER
    de = ldro_for(sf, bw)
    numerator = 8 * total - 4 * sf + 28 + 16
    denominator = 4 * (sf - 2 * de)
    # The 8 belongs to the payload symbol count, not to the preamble term as well.
    n_payload_symbols = 8 + max(math.ceil(numerator / denominator) * cr, 0)
    t_sym = (2**sf) / bw
    return (PREAMBLE + 4.25 + n_payload_symbols) * t_sym


def toa_radiolib(payload_bytes, preset):
    """Seconds for one frame, by RadioLib's integer path in SX126x::calculateTimeOnAir."""
    sf, bw, cr = PRESETS[preset]
    total = payload_bytes + MESHTASTIC_HEADER

    symbol_length_us = ((1000 * 10) << sf) // (bw // 1000 * 10)
    sf_coeff1_x4 = 17  # 4.25 * 4
    sf_coeff2 = 8
    if sf in (5, 6):
        sf_coeff1_x4, sf_coeff2 = 25, 0

    sf_divisor = 4 * sf
    if ldro_for(sf, bw):
        sf_divisor = 4 * (sf - 2)

    bit_count = 8 * total + 16 - 4 * sf + sf_coeff2 + 20  # CRC on, explicit header
    bit_count = max(bit_count, 0)
    n_precoded = (bit_count + sf_divisor - 1) // sf_divisor
    n_symbol_x4 = (PREAMBLE + 8) * 4 + sf_coeff1_x4 + n_precoded * cr * 4
    return (symbol_length_us * n_symbol_x4 / 4) / 1e6


def frames(payload_bytes):
    """Frames a payload needs. Airtime is charged per frame, not per byte."""
    return max(1, math.ceil(payload_bytes / PAYLOAD_BUDGET))


def airtime(payload_bytes, preset, toa=toa_radiolib):
    """Airtime for a whole message, fragmented across frames when it does not fit."""
    whole, rest = divmod(payload_bytes, PAYLOAD_BUDGET)
    total = whole * toa(PAYLOAD_BUDGET, preset)
    if rest or not whole:
        total += toa(rest, preset)
    return total


def bytes_per_hour_at_utilisation(fraction, frame_bytes, preset):
    """Payload bytes an hour that occupy `fraction` of the channel.

    Airtime is per frame, not per byte, so this depends on the frame size carrying it -
    a stream of 43-byte adverts buys fewer bytes per second than the same airtime spent
    on full frames. Quote the frame size alongside any figure derived from this.
    """
    per_frame = airtime(frame_bytes, preset)
    return (3600.0 * fraction / per_frame) * frame_bytes
