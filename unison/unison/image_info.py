"""Header-only image metadata; no pixel decoding or content validation."""
import hashlib


def image_info(data: bytes) -> dict:
    """Inspect PNG, JPEG, GIF or WebP bytes, independent of the filename.

    Dimensions describe the stored image/canvas (EXIF orientation is not applied).
    A successful result does not certify that the entire image is decodable.
    """
    def number(start, size, order='big'):
        if start + size > len(data):
            raise ValueError('Truncated image header')
        return int.from_bytes(data[start:start + size], order)

    fmt = None
    width = height = 0
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        if len(data) < 33 or data[12:16] != b'IHDR' or number(8, 4) != 13:
            raise ValueError('Invalid or truncated PNG IHDR')
        fmt, width, height = 'PNG', number(16, 4), number(20, 4)
    elif data[:6] in (b'GIF87a', b'GIF89a'):
        if len(data) < 13:
            raise ValueError('Truncated GIF header')
        fmt, width, height = 'GIF', number(6, 2, 'little'), number(8, 2, 'little')
    elif data.startswith(b'\xff\xd8'):
        # SOF markers, excluding DHT (C4), JPG (C8), and DAC (CC).
        sof = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
               0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
        pos = 2
        while pos < len(data):
            if data[pos] != 0xff:
                raise ValueError('Invalid JPEG marker')
            while pos < len(data) and data[pos] == 0xff:
                pos += 1
            if pos == len(data):
                break
            marker = data[pos]
            pos += 1
            if marker in (0xd9, 0xda):
                break
            if marker == 0x01 or 0xd0 <= marker <= 0xd7:
                continue
            length = number(pos, 2)
            if length < 2 or pos + length > len(data):
                raise ValueError('Invalid or truncated JPEG segment')
            if marker in sof:
                if length < 8 or length != 8 + 3 * data[pos + 7]:
                    raise ValueError('Invalid JPEG frame header')
                fmt, width, height = 'JPEG', number(pos + 5, 2), number(pos + 3, 2)
                break
            pos += length
        if fmt is None:
            raise ValueError('JPEG frame dimensions not found')
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        end = number(4, 4, 'little') + 8
        if end > len(data) or end < 12:
            raise ValueError('Invalid or truncated WebP container')
        pos = 12
        while pos + 8 <= end:
            kind = data[pos:pos + 4]
            length = number(pos + 4, 4, 'little')
            start = pos + 8
            if start + length + (length & 1) > end:
                raise ValueError('Invalid or truncated WebP chunk')
            if kind == b'VP8X':
                if length != 10:
                    raise ValueError('Invalid WebP VP8X header')
                width = number(start + 4, 3, 'little') + 1
                height = number(start + 7, 3, 'little') + 1
            elif kind == b'VP8L':
                if length < 5 or data[start] != 0x2f:
                    raise ValueError('Invalid WebP VP8L header')
                bits = number(start + 1, 4, 'little')
                width, height = (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
            elif kind == b'VP8 ':
                if length < 10 or data[start] & 1 or data[start + 3:start + 6] != b'\x9d\x01\x2a':
                    raise ValueError('Invalid WebP VP8 frame header')
                width = number(start + 6, 2, 'little') & 0x3fff
                height = number(start + 8, 2, 'little') & 0x3fff
            if width and height:
                fmt = 'WebP'
                break
            pos = start + length + (length & 1)
        if fmt is None:
            raise ValueError('WebP image dimensions not found')
    else:
        raise ValueError('Unsupported image format (expected PNG, JPEG, WebP or GIF)')
    if width <= 0 or height <= 0:
        raise ValueError('Invalid image dimensions')
    return {'format': fmt, 'width': width, 'height': height,
            'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
