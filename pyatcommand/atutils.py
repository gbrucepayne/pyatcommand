"""Various utilities/helpers for NIMO modem interaction and debugging.
"""
import os
from datetime import datetime, timezone
from typing import Iterable
from .atconstants import AT_CR, AT_LF, AT_BS, AT_SEP, AT_CRC_SEP


class AtConfig:
    """Configuration settings for a modem."""
    def __init__(self) -> None:
        self.echo: bool = True
        self.verbose: bool = True
        self.quiet: bool = False
        self.crc: bool = False
        self.cr: int = AT_CR
        self.lf: int = AT_LF
        self.bs: int = AT_BS
        self.sep: int = AT_SEP
        self.crc_sep: int = AT_CRC_SEP
    
    @property
    def terminator(self) -> str:
        return f'{self.cr}{self.lf}'


def printable_char(c: int, debug: bool = False) -> bool:
    """Determine if a character is printable.
    
    Args:
        debug: If True prints the character or byte value to stdout
    """
    printable = True
    to_print: str = ''
    if c == ord('\b'):
        to_print = '<bs>'
    elif c == ord('\n'):
        to_print = '<lf>'
    elif c == ord('\r'):
        to_print = '<cr>'
    elif (c < 32 or c > 125):
        printable = False
        to_print = f'[{c}]'
    else:
        to_print = chr(c)
    if debug:
        print(to_print, end='')
    return printable


def dprint(raw_string: str) -> str:
    """Get a printable string on a single line."""
    printable = raw_string.replace('\b', '<bs>')
    printable = printable.replace('\n', '<lf>')
    printable = printable.replace('\r', '<cr>')
    return printable


def vlog(tag: str) -> bool:
    """Returns True if the tag is in the LOG_VERBOSE environment variable."""
    if not isinstance(tag, str) or tag == '':
        return False
    return tag in str(os.getenv('LOG_VERBOSE'))


def ts_to_iso(timestamp: 'float|int', ms: bool = False) -> str:
    """Converts a unix timestamp to ISO 8601 format (UTC).
    
    Args:
        timestamp: A unix timestamp.
        ms: Flag indicating whether to include milliseconds in response
    
    Returns:
        ISO 8601 UTC format e.g. `YYYY-MM-DDThh:mm:ss[.sss]Z`

    """
    iso_time = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    if not ms:
        return f'{iso_time[:19]}Z'
    return f'{iso_time[:23]}Z'


def iso_to_ts(iso_time: str, ms: bool = False) -> int:
    """Converts a ISO 8601 timestamp (UTC) to unix timestamp.
    
    Args:
        iso_time: An ISO 8601 UTC datetime `YYYY-MM-DDThh:mm:ss[.sss]Z`
        ms: Flag indicating whether to include milliseconds in response
    
    Returns:
        Unix UTC timestamp as an integer, or float if `ms` flag is set.

    """
    if '.' not in iso_time:
        iso_time = iso_time.replace('Z', '.000Z')
    utc_dt = datetime.strptime(iso_time, '%Y-%m-%dT%H:%M:%S.%fZ')
    ts = (utc_dt - datetime(1970, 1, 1)).total_seconds()
    if not ms:
        ts = int(ts)
    return ts


def bits_in_bitmask(bitmask: int) -> Iterable[int]:
    """Get iterable integer value of each bit in a bitmask."""
    while bitmask:
        bit = bitmask & (~bitmask+1)
        yield bit
        bitmask ^= bit
