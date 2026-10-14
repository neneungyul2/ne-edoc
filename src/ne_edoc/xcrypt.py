"""그룹웨어(XClick) 로그인용 파라미터 암호화.

브라우저의 jQuery.encrypt 를 그대로 옮긴 것이다.
(키 'xclick6' 기반 XXTEA 변형, 3라운드, delta=123)
"""
import base64

_KEY = "xclick6"


def _i32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def _utf8(s: str) -> str:
    # JS Utf8.encode: 문자 단위로 UTF-8 바이트를 만든 뒤 1바이트 = 1문자로 표현
    return s.encode("utf-8").decode("latin-1")


def _str_to_longs(s: str) -> list:
    n = (len(s) + 3) // 4
    out = []
    for i in range(n):
        v = 0
        for j in range(4):
            k = i * 4 + j
            if k < len(s):
                v += ord(s[k]) << (8 * j) if j < 3 else _i32(ord(s[k]) << 24)
        out.append(v)
    return out


def _longs_to_str(l: list) -> bytes:
    b = bytearray()
    for x in l:
        u = x & 0xFFFFFFFF
        b += bytes([u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF, (u >> 24) & 0xFF])
    return bytes(b)


def encrypt(plain: str, key: str = _KEY) -> str:
    if not plain:
        return ""
    v = _str_to_longs(_utf8(plain))
    if len(v) <= 1:
        v.append(0)
    k = _str_to_longs(_utf8(key)[:16])
    n = len(v)
    z = v[n - 1]
    delta = 123
    total = 0
    for _ in range(3):
        total += delta
        e = ((total & 0xFFFFFFFF) >> 2) & 3
        for p in range(n):
            y = v[(p + 1) % n]
            kk = k[(p & 3) ^ e] if ((p & 3) ^ e) < len(k) else 0
            a = (_i32(z) ^ _i32(y)) + (_i32(y) ^ _i32(z))
            b = (_i32(total) ^ _i32(y)) + (_i32(kk) ^ _i32(z))
            c = _i32(a) ^ _i32(b)
            v[p] = v[p] + c
            z = v[p]
    enc = base64.b64encode(_longs_to_str(v)).decode("ascii")
    return enc.replace("=", "!").replace("\r\n", "")
