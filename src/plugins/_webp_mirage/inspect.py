"""Read RIFF structure without exporting image pixels or unrelated EXIF data."""
from pathlib import Path
import json
from PIL import Image


def _chunks(data, start=12):
    offset = start
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('Truncated chunk header')
        tag = data[offset:offset + 4].decode('ascii', errors='replace')
        length = int.from_bytes(data[offset + 4:offset + 8], 'little')
        end = offset + 8 + length
        if end + (length & 1) > len(data):
            raise ValueError('Truncated chunk')
        yield tag, data[offset + 8:end], offset
        offset = end + (length & 1)


def inspect_webp(path) -> dict:
    path = Path(path)
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError('File exceeds inspection size limit')
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WEBP':
        raise ValueError('Not a RIFF WebP file')
    if int.from_bytes(data[4:8], 'little') + 8 != len(data):
        raise ValueError('RIFF size mismatch')
    result = {'file_bytes': len(data), 'chunks': [], 'frames': []}
    for tag, payload, offset in _chunks(data):
        result['chunks'].append({'tag': tag, 'bytes': len(payload), 'offset': offset})
        if tag == 'VP8X':
            if len(payload) != 10:
                raise ValueError('Invalid VP8X')
            result['flags'] = payload[0]
            result['canvas'] = [int.from_bytes(payload[4:7], 'little') + 1,
                                int.from_bytes(payload[7:10], 'little') + 1]
        elif tag == 'ANIM':
            if len(payload) != 6:
                raise ValueError('Invalid ANIM')
            result['loops'] = int.from_bytes(payload[4:6], 'little')
            result['background_bgra'] = list(payload[:4])
        elif tag == 'ANMF':
            if len(payload) < 16:
                raise ValueError('Invalid ANMF')
            values = [int.from_bytes(payload[i:i+3], 'little') for i in range(0, 15, 3)]
            result['frames'].append({'x': 2 * values[0], 'y': 2 * values[1],
                'width': values[2] + 1, 'height': values[3] + 1, 'duration_ms': values[4],
                'flags': payload[15], 'blend': not bool(payload[15] & 2),
                'dispose_to_background': bool(payload[15] & 1),
                'subchunks': [{'tag': t, 'bytes': len(p)} for t, p, _ in _chunks(payload, 16)]})
        elif tag == 'EXIF':
            exif = Image.Exif()
            exif.load(payload)
            description = exif.get(270, '')
            if isinstance(description, str) and description.startswith('hbe:'):
                result['hbe'] = json.loads(description[4:])
        elif tag == 'HBEF':
            result['padding_bytes'] = len(payload)
            result['padding_all_zero'] = not any(payload)
    return result
