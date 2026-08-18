"""PinSketch over GF(2^32), ported from src/modules/Native/PinSketch.cpp.

This is a transcription, not a reimplementation: it exists so the simulator's sketches are the
same bytes the firmware would put on the air. Where the two could drift, oracle.py compiles the
C++ and compares, so a divergence fails a test rather than quietly changing a result.

Field: GF(2^32) modulo x^32 + x^7 + x^3 + x^2 + 1.

The caches below are memoisation of pure functions, not approximations: every byte this module
produces is unchanged, which check_oracle.py verifies against the firmware's own PinSketch.cpp.
"""

from functools import lru_cache

MODULUS = 0x8D
FIELD_BITS = 32
MASK = 0xFFFFFFFF


def mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        b >>= 1
        overflow = a & 0x80000000
        a = (a << 1) & MASK
        if overflow:
            a ^= MODULUS
    return r


def sqr(a):
    return mul(a, a)


# 62 multiplications a call, and the polynomial routines below ask for the same handful of
# leading coefficients over and over: measured 13863 hits against 281 misses in a single run.
@lru_cache(maxsize=1 << 16)
def inv(a):
    # a^(2^32-2), the exponent being the sum of 2^1..2^31.
    r, p = 1, a
    for _ in range(1, FIELD_BITS):
        p = sqr(p)
        r = mul(r, p)
    return r


# --- Polynomials over GF(2^32), coefficient index == degree, no trailing zeros ---


def _trim(p):
    while p and p[-1] == 0:
        p.pop()
    return p


def _deg(p):
    return len(p) - 1


def _add_to(a, b):
    if len(a) < len(b):
        a.extend([0] * (len(b) - len(a)))
    for i, coef in enumerate(b):
        a[i] ^= coef
    return _trim(a)


def _mul_poly(a, b):
    if not a or not b:
        return []
    r = [0] * (len(a) + len(b) - 1)
    for i, ai in enumerate(a):
        if ai == 0:
            continue
        for j, bj in enumerate(b):
            r[i + j] ^= mul(ai, bj)
    return _trim(r)


def _divmod_poly(num, den, want_quotient=False):
    """Long division. Returns (remainder, quotient); quotient is [] unless asked for."""
    if not den:
        return [], []
    rem = _trim(list(num))
    dd = _deg(den)
    # A monic divisor is the common case here, and needs no inverse at all.
    lead_inv = 1 if den[-1] == 1 else inv(den[-1])
    quotient = [0] * (_deg(rem) - dd + 1) if want_quotient and _deg(rem) >= dd else []

    while rem and _deg(rem) >= dd:
        shift = _deg(rem) - dd
        coef = mul(rem[-1], lead_inv)
        if want_quotient:
            quotient[shift] = coef
        for i in range(dd + 1):
            rem[shift + i] ^= mul(coef, den[i])
        _trim(rem)
    return rem, _trim(quotient)


def _gcd_poly(a, b):
    a, b = _trim(list(a)), _trim(list(b))
    while b:
        r, _ = _divmod_poly(a, b)
        a, b = b, r
    return a


def _trace_poly(beta, f):
    """Tr(beta*z) reduced mod f, where Tr(y) = y + y^2 + y^4 + ... + y^(2^31)."""
    h, _ = _divmod_poly([0, beta], f)
    acc = list(h)
    for _ in range(1, FIELD_BITS):
        h, _ = _divmod_poly(_mul_poly(h, h), f)
        _add_to(acc, h)
    return acc


def _find_roots(f, out):
    """Berlekamp trace algorithm. False if f does not split completely over the field."""
    deg = _deg(f)
    if deg <= 0:
        return (
            deg == 0
        )  # nonzero constant has no roots; the zero polynomial is not decodable
    if deg == 1:
        out.append(mul(f[0], inv(f[1])))
        return True
    for k in range(FIELD_BITS):
        g = _gcd_poly(f, _trace_poly(1 << k, f))
        dg = _deg(g)
        if 0 < dg < deg:
            _, q = _divmod_poly(f, g, want_quotient=True)
            return _find_roots(g, out) and _find_roots(q, out)
    return False


def _berlekamp_massey(s):
    """Shortest connection polynomial over the syndrome sequence."""
    c, b = [1], [1]
    l, m = 0, 1
    bb = 1

    for n in range(len(s)):
        d = s[n]
        for i in range(1, min(l, len(c) - 1) + 1):
            d ^= mul(c[i], s[n - i])
        if d == 0:
            m += 1
            continue
        prev = list(c)
        coef = mul(d, inv(bb))
        if len(c) < len(b) + m:
            c.extend([0] * (len(b) + m - len(c)))
        for i, bi in enumerate(b):
            c[i + m] ^= mul(coef, bi)
        if 2 * l <= n:
            l = n + 1 - l
            b = prev
            bb = d
            m = 1
        else:
            m += 1
    return _trim(c)


# The same short ID is added by every server that hears it, and again by decode()'s verification:
# measured 1584 adds over 98 distinct (element, capacity) pairs in one run.
@lru_cache(maxsize=1 << 16)
def _odd_powers(e, capacity):
    """e, e^3, e^5 ... e^(2*capacity-1). Returned shared and read-only - never mutate it."""
    step = sqr(e)
    out = []
    power = e
    for _ in range(capacity):
        out.append(power)
        power = mul(power, step)
    return tuple(out)


class Sketch:
    """A fixed-size digest holding the odd power sums of its members."""

    def __init__(self, capacity=0):
        self.syndromes = [0] * capacity

    @property
    def capacity(self):
        return len(self.syndromes)

    def serialized_size(self):
        return len(self.syndromes) * 4

    def add(self, e):
        """Toggles membership - adding a held element removes it again. Rejects zero."""
        if e == 0:
            return False
        syndromes = self.syndromes
        for i, power in enumerate(_odd_powers(e, len(syndromes))):
            syndromes[i] ^= power
        return True

    def merge(self, other):
        """XORs an equal-capacity sketch in, leaving the sketch of the symmetric difference."""
        if len(other.syndromes) != len(self.syndromes):
            return False
        for i in range(len(self.syndromes)):
            self.syndromes[i] ^= other.syndromes[i]
        return True

    def empty(self):
        return all(s == 0 for s in self.syndromes)

    def clear(self):
        self.syndromes = [0] * len(self.syndromes)

    def truncate(self, new_capacity):
        """Discards capacity from the tail. Only ever shrinks - what makes prefix streaming work."""
        if new_capacity < len(self.syndromes):
            self.syndromes = self.syndromes[:new_capacity]

    def copy(self):
        s = Sketch(0)
        s.syndromes = list(self.syndromes)
        return s

    def serialize(self):
        out = bytearray()
        for e in self.syndromes:
            out += bytes(
                ((e) & 0xFF, (e >> 8) & 0xFF, (e >> 16) & 0xFF, (e >> 24) & 0xFF)
            )
        return bytes(out)

    def deserialize(self, data):
        if len(data) % 4 != 0:
            return False
        self.syndromes = [
            int.from_bytes(data[i : i + 4], "little") for i in range(0, len(data), 4)
        ]
        return True

    def decode(self):
        """Recovers the set, sorted ascending, or None when it is not decodable.

        Subject to the ~1/c! misdecode rate: an over-capacity difference lands on a wrong set that
        reproduces the same syndromes at about that rate.
        """
        if not self.syndromes:
            return None
        if self.empty():
            return []

        # Odd syndromes are carried; even ones are free, since S_2j == S_j^2 in characteristic 2.
        c = len(self.syndromes)
        s = [0] * (2 * c)
        for j in range(1, 2 * c + 1):
            s[j - 1] = self.syndromes[(j - 1) // 2] if j & 1 else sqr(s[j // 2 - 1])

        locator = _berlekamp_massey(s)
        if not locator or locator[0] != 1 or _deg(locator) > c:
            return None

        roots = []
        if not _find_roots(locator, roots) or len(roots) != _deg(locator):
            return None

        found = []
        for r in roots:
            if r == 0:
                return None
            found.append(inv(r))
        found.sort()
        if any(found[i] == found[i + 1] for i in range(len(found) - 1)):
            return None

        # The locator is only a candidate until the recovered set reproduces the syndromes we
        # started from. Without this an over-capacity difference decodes to a plausible wrong set.
        check = Sketch(c)
        for e in found:
            check.add(e)
        if check.syndromes != self.syndromes:
            return None
        return found
