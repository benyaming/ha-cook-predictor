"""V8's Math.exp and Math.log in Python, bit for bit, for the estimator golden tests.

The estimator calls the platform's math.exp and math.log, which differ from V8 in the
last bit for about 1% of exp arguments (and differ between platforms). The golden tests
swap these in so the port can be compared with the JavaScript exactly.

Ports of V8's src/base/ieee754.cc (FreeBSD msun, from fdlibm), operation for operation:
Python floats are IEEE doubles with the same rounding, so the results match bit for bit.
The C code branches on the high word of |x|; those tests are made here on |x| against the
smallest double with that high word, which orders the same way.

V8 is compiled by clang with -ffp-contract=on: within one C expression a multiply that
feeds an add or a subtract is fused into a single fma (the left operand's multiply when
both are). The goldens were recorded from such a build (Node 25 on arm64), and math.fma
rounds the same way on every platform, so the fused steps are written as math.fma. Checked
against 1.3 million V8 results, random and special arguments, with no difference.
"""

import math
import struct

_fma = math.fma

_EXP_BIG = float.fromhex("0x1.62e42p+9")  # high word 0x40862E42: |x| >= 709.78...
_EXP_REDUCE = float.fromhex("0x1.62e43p-2")  # high word above 0x3fd62e42: |x| > 0.5 ln2
_EXP_ONE_STEP = float.fromhex("0x1.0a2b2p+0")  # high word 0x3FF0A2B2: |x| < 1.5 ln2
_EXP_TINY = float.fromhex("0x1p-28")  # high word 0x3e300000: |x| < 2**-28
_HALF = (0.5, -0.5)
_O_THRESHOLD = 7.09782712893383973096e02  # 0x40862E42, 0xFEFA39EF
_U_THRESHOLD = -7.45133219101941108420e02  # 0xc0874910, 0xD52D3051
_LN2_HI = (6.93147180369123816490e-01, -6.93147180369123816490e-01)  # 0x3fe62e42, 0xfee00000
_LN2_LO = (1.90821492927058770002e-10, -1.90821492927058770002e-10)  # 0x3dea39ef, 0x35793c76
_INV_LN2 = 1.44269504088896338700e00  # 0x3ff71547, 0x652b82fe
_P1 = 1.66666666666666019037e-01  # 0x3FC55555, 0x5555553E
_P2 = -2.77777777770155933842e-03  # 0xBF66C16C, 0x16BEBD93
_P3 = 6.61375632143793436117e-05  # 0x3F11566A, 0xAF25DE2C
_P4 = -1.65339022054652515390e-06  # 0xBEBBBD41, 0xC5D26BF1
_P5 = 4.13813679705723846039e-08  # 0x3E663769, 0x72BEA4D0
_E = 2.718281828459045  # 0x4005bf0a, 0x8b145769
_HUGE = 1.0e300
_TWOM1000 = 9.33263618503218878990e-302  # 2**-1000
_TWO1023 = 8.988465674311579539e307  # 2**1023


def exp(x: float) -> float:
    ax = abs(x)
    xsb = 1 if x < 0 else 0  # sign bit of x
    # filter out non-finite argument
    if not ax < _EXP_BIG:  # if |x|>=709.78... (or NaN)
        if math.isnan(x):
            return x + x  # NaN
        if ax == math.inf:
            return x if xsb == 0 else 0.0  # exp(+-inf)={inf,0}
        if x > _O_THRESHOLD:
            return _HUGE * _HUGE  # overflow
        if x < _U_THRESHOLD:
            return _TWOM1000 * _TWOM1000  # underflow
    # argument reduction
    hi = lo = 0.0
    k = 0
    if ax >= _EXP_REDUCE:  # if  |x| > 0.5 ln2
        if ax < _EXP_ONE_STEP:  # and |x| < 1.5 ln2
            # V8 special-cases exp(1) to return E: the code below gets its last bit wrong.
            if x == 1.0:
                return _E
            hi = x - _LN2_HI[xsb]
            lo = _LN2_LO[xsb]
            k = 1 - xsb - xsb
        else:
            k = int(_fma(_INV_LN2, x, _HALF[xsb]))  # (int)(invln2*x+halF[xsb])
            t = float(k)
            hi = _fma(-t, _LN2_HI[0], x)  # x - t*ln2HI: t*ln2HI is exact here
            lo = t * _LN2_LO[0]
        x = hi - lo
    elif ax < _EXP_TINY and _HUGE + x > 1.0:  # when |x|<2**-28
        return 1.0 + x  # trigger inexact
    # x is now in primary range
    t = x * x
    # c = x - t*(P1+t*(P2+t*(P3+t*(P4+t*P5))))
    c = _fma(-t, _fma(t, _fma(t, _fma(t, _fma(t, _P5, _P4), _P3), _P2), _P1), x)
    if k == 0:
        return 1.0 - ((x * c) / (c - 2.0) - x)
    y = 1.0 - ((lo - (x * c) / (2.0 - c)) - hi)
    if k >= -1021:
        if k == 1024:
            return y * 2.0 * _TWO1023
        return y * math.ldexp(1.0, k)
    return y * math.ldexp(1.0, k + 1000) * _TWOM1000


_LN2_HI0 = 6.93147180369123816490e-01  # 3fe62e42 fee00000
_LN2_LO0 = 1.90821492927058770002e-10  # 3dea39ef 35793c76
_TWO54 = 1.80143985094819840000e16  # 43500000 00000000
_LG1 = 6.666666666666735130e-01  # 3FE55555 55555593
_LG2 = 3.999999999940941908e-01  # 3FD99999 9997FA04
_LG3 = 2.857142874366239149e-01  # 3FD24924 94229359
_LG4 = 2.222219843214978396e-01  # 3FCC71C5 1D8E78AF
_LG5 = 1.818357216161805012e-01  # 3FC74664 96CB03DE
_LG6 = 1.531383769920937332e-01  # 3FC39A09 D078C69F
_LG7 = 1.479819860511658591e-01  # 3FC2F112 DF3E5244


def _words(x: float) -> tuple[int, int]:
    """The signed high and the unsigned low 32-bit words of a double."""
    bits = struct.unpack("<q", struct.pack("<d", x))[0]
    return bits >> 32, bits & 0xFFFFFFFF


def log(x: float) -> float:
    hx, lx = _words(x)
    k = 0
    if hx < 0x00100000:  # x < 2**-1022
        if ((hx & 0x7FFFFFFF) | lx) == 0:
            return -math.inf  # log(+-0)=-inf
        if hx < 0:
            return math.nan  # log(-#) = NaN
        k -= 54
        x *= _TWO54  # subnormal number, scale up x
        hx, lx = _words(x)
    if hx >= 0x7FF00000:
        return x + x
    k += (hx >> 20) - 1023
    hx &= 0x000FFFFF
    i = (hx + 0x95F64) & 0x100000
    x = struct.unpack("<d", struct.pack("<II", lx, hx | (i ^ 0x3FF00000)))[0]  # normalize x or x/2
    k += i >> 20
    f = x - 1.0
    if (0x000FFFFF & (2 + hx)) < 3:  # -2**-20 <= f < 2**-20
        if f == 0:
            if k == 0:
                return 0.0
            dk = float(k)
            return _fma(dk, _LN2_HI0, dk * _LN2_LO0)  # dk*ln2_hi+dk*ln2_lo
        r = f * f * _fma(-0.33333333333333333, f, 0.5)  # f*f*(0.5-0.33333333333333333*f)
        if k == 0:
            return f - r
        dk = float(k)
        return _fma(dk, _LN2_HI0, -(_fma(-dk, _LN2_LO0, r) - f))  # dk*ln2_hi-((R-dk*ln2_lo)-f)
    s = f / (2.0 + f)
    dk = float(k)
    z = s * s
    i = hx - 0x6147A
    w = z * z
    j = 0x6B851 - hx
    t1 = w * _fma(w, _fma(w, _LG6, _LG4), _LG2)  # w*(Lg2+w*(Lg4+w*Lg6))
    t2 = z * _fma(w, _fma(w, _fma(w, _LG7, _LG5), _LG3), _LG1)  # z*(Lg1+w*(Lg3+w*(Lg5+w*Lg7)))
    i |= j
    r = t2 + t1
    if i > 0:
        hfsq = 0.5 * f * f
        if k == 0:
            return f - _fma(-s, hfsq + r, hfsq)  # f-(hfsq-s*(hfsq+R))
        # dk*ln2_hi-((hfsq-(s*(hfsq+R)+dk*ln2_lo))-f)
        return _fma(dk, _LN2_HI0, -((hfsq - _fma(s, hfsq + r, dk * _LN2_LO0)) - f))
    if k == 0:
        return _fma(-s, f - r, f)  # f-s*(f-R)
    # dk*ln2_hi-((s*(f-R)-dk*ln2_lo)-f)
    return _fma(dk, _LN2_HI0, -(_fma(s, f - r, -(dk * _LN2_LO0)) - f))
