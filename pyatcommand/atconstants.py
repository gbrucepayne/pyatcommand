"""Constants for AT command module.
"""
from enum import IntEnum

AT_TIMEOUT = 0.3   # default response timeout in seconds
AT_URC_TIMEOUT = 0.3   # default URC timeout in seconds

AT_CR = '\r'
AT_LF = '\n'
AT_BS = '\b'
AT_SEP = ';'
AT_CRC_SEP = '*'


class AtErrorCode(IntEnum):
    """Error codes returned by a modem."""
    OK = 0   # V.25 standard
    URC = 2   # repurpose V.25 `RING` for unsolicited result codes (URC)
    ERR_TIMEOUT = 3   # repurpose V.25 `NO CARRIER` for modem unavailable
    ERROR = 4   # V.25 standard
    # ORBCOMM satellite modem compatible
    ERR_CMD_CRC = 100
    ERR_CMD_UNKNOWN = 101
    # Custom definitions for this library
    ERR_BAD_BYTE = 255
    ERR_CRC_CONFIG = 254
    PENDING = 253


class AtParsing(IntEnum):
    """States for AT response parsing."""
    NONE = 0
    ECHO = 1
    RESPONSE = 2
    CRC = 3
    OK = 4
    ERROR = 5
    COMMAND = 6
