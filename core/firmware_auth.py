"""Compatibility with the inspected web UI's Blowfish AUTHKEY, not a new cipher.
No downloaded JS is executed. Constants are the standard public Blowfish tables.
The fixed protocol key is NOT a BMC password; actual credentials remain in memory.
"""
import base64
import hashlib
import json
from functools import lru_cache
from pathlib import Path

MASK = 0xffffffff

class Blowfish:
    def __init__(self, key):
        raw = Path(__file__).with_name('blowfish_constants.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != '2ec3e89ec0c2d0060ec65de8e45cd97051e283036922261ddb6842bc645321b2':
            raise ValueError('Blowfish 常量文件校验失败')
        constants = json.loads(raw)
        self.p, self.s = constants['P'], constants['S']
        for i in range(18):
            word = int.from_bytes(bytes(key[(i * 4 + j) % len(key)] for j in range(4)), 'big')
            self.p[i] ^= word
        left = right = 0
        for i in range(0, 18, 2):
            left, right = self.encipher(left, right)
            self.p[i:i+2] = [left, right]
        for box in self.s:
            for i in range(0, 256, 2):
                left, right = self.encipher(left, right)
                box[i:i+2] = [left, right]

    def encipher(self, left, right):
        for i in range(16):
            left ^= self.p[i]
            a, b, c, d = (left >> 24) & 255, (left >> 16) & 255, (left >> 8) & 255, left & 255
            f = (((self.s[0][a] + self.s[1][b]) & MASK) ^ self.s[2][c]) + self.s[3][d]
            left, right = (right ^ f) & MASK, left
        left, right = right, left
        return (left ^ self.p[17]) & MASK, (right ^ self.p[16]) & MASK

    def encrypt(self, data):
        if len(data) % 8:
            raise ValueError('Blowfish block alignment')
        result = bytearray()
        for i in range(0, len(data), 8):
            left, right = self.encipher(int.from_bytes(data[i:i+4], 'big'), int.from_bytes(data[i+4:i+8], 'big'))
            result += left.to_bytes(4, 'big') + right.to_bytes(4, 'big')
        return bytes(result)

@lru_cache(maxsize=1)
def _protocol_cipher():
    return Blowfish(b'megarac')

def confirmation_key(password):
    # Match encrypt(password, 24); do not silently truncate longer/Unicode passwords.
    if not password or len(password) > 24 or not password.isascii() or '\x00' in password:
        raise ValueError('本适配器的二次确认密码仅支持 1～24 位 ASCII 且不含 NUL；不截断密码')
    return base64.b64encode(_protocol_cipher().encrypt(password.encode('ascii').ljust(24, b'\0'))).decode('ascii')
