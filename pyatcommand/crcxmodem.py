"""Implementation of CCIT-16-CRC for use with NIMO modems.

This module enables CRC error checking on a serial AT command interface, useful
for increasing robustness in electrically noisy environments or long cable runs.

"""
import logging

from .utils import dprint, vlog

POLYNOMIAL = 0x1021
CRC_SEP = '*'
VLOG_TAG = 'crcxmodem'

_log = logging.getLogger(__name__)
_crcxmodem_table: 'list[int]' = []
_crc_table_is_initialized: bool = False


def apply_crc(at_command: str, sep: str = CRC_SEP) -> str:
    """Applies a CRC-16-CCITT checksum to the at_command."""
    crc = _calculate_crc(at_command)
    hex_crc = f'{crc:04X}'
    if vlog(VLOG_TAG):
        _log.debug('Applying CRC: %d -> %s', crc, hex_crc)
    return at_command + sep + hex_crc


def validate_crc(response: str, sep = CRC_SEP) -> bool:
    """Validates a modem response with checksum."""
    if sep not in response:
        _log.warning('No CRC in response %s', dprint(response))
        return False
    if vlog(VLOG_TAG):
        _log.debug('Validating CRC for %s', dprint(response))
    res, res_crc = response.rsplit(sep, 1)
    expected = _calculate_crc(res)
    received = int(res_crc, 16)
    return expected == received


def _initial_crc(c: int) -> int:
    """Generates an initial CRC for each element in the table."""
    crc = 0
    c = c << 8
    for i in range(8):
        if (crc ^ c) & 0x8000:
            crc = (crc << 1) ^ POLYNOMIAL
        else:
            crc = crc << 1
        c = c << 1
    return crc


def _initialize_crc_table() -> bool:
    """Initializes the table of CRC values for lookup."""
    global _crcxmodem_table
    global _crc_table_is_initialized
    if not _crc_table_is_initialized:
        if vlog(VLOG_TAG):
            _log.debug('Initializing CRC table')
        _crcxmodem_table = [_initial_crc(i) for i in range(256)]
        _crc_table_is_initialized = True
    return _crc_table_is_initialized


def _update_crc(crc: int, c: int) -> int:
    """Call iteratively to build a CRC a character at a time."""
    global _crcxmodem_table
    cc = 0xFF & c
    tmp = (crc >> 8) ^ cc
    crc = (crc << 8) ^ _crcxmodem_table[tmp & 0xFF]
    crc = crc & 0xFFFF
    return crc

def _calculate_crc(string: str, initial_value: int = 0xFFFF) -> int:
    """Calculates the CRC of a string."""
    _initialize_crc_table()
    crc: int = initial_value
    for c in string:
        crc = _update_crc(crc, ord(c))
    return crc
