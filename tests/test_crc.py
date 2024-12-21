import logging

from pyatcommand.crcxmodem import apply_crc, validate_crc


logger = logging.getLogger(__name__)

validation = {
    'AT': 'AT*3983',
    'at': 'at*1B07',
    'AT%CRC=0': 'AT%CRC=0*BBEB',
    'at%crc=0': 'at%crc=0*1749',
    '\r\nOK\r\n': '\r\nOK\r\n*86C5',
    '\r\nERROR\r\n': '\r\nERROR\r\n*84D9',
    '0\r': '0\r*C937',
    '4\r': '4\r*05F3',
}


def test_apply_crc():
    for k, v in validation.items():
        assert apply_crc(k) == v


def test_validate_crc():
    for v in validation.values():
        assert validate_crc(v, '*')