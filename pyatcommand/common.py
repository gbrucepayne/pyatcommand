"""Various utilities/helpers for NIMO modem interaction and debugging.
"""
import base64
import binascii
import glob
import inspect
import logging
import os
import platform
import subprocess
from datetime import datetime, timezone
from enum import IntEnum
from string import printable
from typing import Iterable, Optional, Union

import serial

AT_TIMEOUT = 0.3   # default response timeout in seconds
AT_URC_TIMEOUT = 0.3   # default URC timeout in seconds

AT_CR = '\r'
AT_LF = '\n'
AT_BS = '\b'
AT_SEP = ';'
AT_CRC_SEP = '*'


_log = logging.getLogger(__name__)


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
    CME_ERROR = 252
    CMS_ERROR = 251


class AtConfig:
    """Configuration settings for a DCE/modem."""
    def __init__(self) -> None:
        self._echo: bool = True
        self._verbose: bool = True
        self._quiet: bool = False
        self._cr: str = AT_CR   # default S3 parameter value
        self._lf: str = AT_LF   # default S4 parameter value
        self._terminator: str = self.cr   # default command terminator
        self._bs: str = AT_BS
        self._sep: str = AT_SEP
        self._crc: Union[bool, None] = None
        self._crc_sep: str = AT_CRC_SEP
        self._reserved = {'?', '=', '+', '%', '#', ' ', '\t', ';'}
    
    def _is_valid_alt_char(self, value: str, max_len: int = 1) -> bool:
        if (not isinstance(value, str) or 
            len(value) < 1 or len(value) > max_len or
            value.isalnum() or
            value in self._reserved):
            return False
        return True
    
    @property
    def cr(self) -> str:
        return self._cr
    
    @cr.setter
    def cr(self, value: str):
        if not self._is_valid_alt_char(value):
            raise ValueError('Invalid S3 must be a valid character')
        self._cr = value
    
    @property
    def lf(self) -> str:
        return self._lf
    
    @lf.setter
    def lf(self, value: str):
        if not self._is_valid_alt_char(value):
            raise ValueError('Invalid S4 must be a valid character')
        self._lf = value
    
    @property
    def sep(self) -> str:
        return self._sep
    
    @property
    def bs(self) -> str:
        return self._bs
    
    @bs.setter
    def bs(self, value: str):
        if not self._is_valid_alt_char(value):
            raise ValueError('Invalid S5 must be a valid character')
        self._bs = value
    
    @property
    def echo(self) -> bool:
        return self._echo
    
    @echo.setter
    def echo(self, enable: bool):
        if not isinstance(enable, bool):
            raise ValueError('Invalid boolean')
        self._echo = enable
    
    @property
    def verbose(self) -> bool:
        return self._verbose
    
    @verbose.setter
    def verbose(self, enable: bool):
        if not isinstance(enable, bool):
            raise ValueError('Invalid boolean')
        self._verbose = enable
    
    @property
    def quiet(self) -> bool:
        return self._quiet
    
    @quiet.setter
    def quiet(self, enable: bool):
        if not isinstance(enable, bool):
            raise ValueError('Invalid boolean')
        self._quiet = enable
    
    @property
    def terminator(self) -> str:
        return self._terminator
    
    @terminator.setter
    def terminator(self, value: str):
        if not self._is_valid_alt_char(value, max_len=2):
            raise ValueError('Invalid command terminator must be non-empty string')
        if len(value) > 1:
            _log.warning('Configuring multi-character command terminator')
        self._terminator = value
        
    @property
    def header(self) -> str:
        """The response info or result header."""
        if self.verbose:
            return f'{self.cr}{self.lf}'
        return ''
    
    @property
    def trailer_result(self) -> str:
        """The response result trailer."""
        if self.verbose:
            return f'{self.cr}{self.lf}'
        return self.cr
    
    @property
    def trailer_info(self) -> str:
        """The response info trailer."""
        return f'{self.cr}{self.lf}'
    
    @property
    def crc(self) -> bool:
        return self._crc
    
    @crc.setter
    def crc(self, enable: bool):
        if not isinstance(enable, bool):
            raise ValueError('Invalid boolean')
        self._crc = enable
        
    @property
    def crc_sep(self) -> str:
        """The CRC indicator to appear after the result code."""
        return self._crc_sep
    
    @crc_sep.setter
    def crc_sep(self, value: str):
        if (not self._is_valid_alt_char(value) or
            value in {self.cr, self.lf}):
            raise ValueError('Invalid CRC separator')
        self._crc_sep = value

    def _asdict(self) -> dict[str, str]:
        members = inspect.getmembers(type(self), 
                                     lambda a: isinstance(a, property))
        return { name: getattr(self, name) for name, attr in members }
    
    def __repr__(self):
        p_str = ''
        for k, v in self._asdict().items():
            if len(p_str) > 0:
                p_str += ', '
            p_str += f'{k}="{v}"' if isinstance(v, str) else f'{k}={v}'
        return f'<{self.__class__.__name__}({p_str})'
    
    def __str__(self) -> str:
        return '\n'.join(f'{k} = {dprint(v) if isinstance(v, str) else v}' 
                         for k, v in self._asdict().items())


class AtResponse:
    """A class defining a response to an AT command.
    
    Attributes:
        result (AtErrorCode): The result code.
        info (str): Information returned or empty string.
        crc_ok (bool): Flag indicating if CRC check passed, if supported.
        elapsed (float): Seconds elapsed between command and response.
        ok (bool): Flag indicating if the result code was a success.
    """
    __slots__ = ('result', 'info', 'crc_ok', 'elapsed', 'raw')
    
    def __init__(self,
                 result: Optional[AtErrorCode] = None,
                 info: Optional[str] = None,
                 crc_ok: Optional[bool] = None,
                 elapsed: Optional[float] = None,
                 raw: Optional[str] = None):
        self.result: Optional[AtErrorCode] = result
        self.info: Optional[str] = info
        self.crc_ok: Optional[bool] = crc_ok
        self.elapsed: Optional[float] = elapsed
        self.raw: Optional[str] = raw
    
    @property
    def ok(self) -> bool:
        if self.crc_ok is not None:
            return self.result == AtErrorCode.OK and self.crc_ok
        return self.result == AtErrorCode.OK


# --------- Helpers / utilities ------------------------------------

_dprint_map = {
    '\r': '<cr>',
    '\n': '<lf>',
    '\b': '<bs>',
    '\t': '<th>',
}


def printable_char(c: int, debug: bool = False) -> bool:
    """Determine if a character is printable.
    
    Args:
        debug: If True prints the character or byte value to stdout
    """
    printable = True
    to_print: str = ''
    if chr(c) in _dprint_map:
        to_print = _dprint_map[chr(c)]
    elif (c < 32 or c > 126):
        printable = False
        to_print = f'[{c}]'
    else:
        to_print = chr(c)
    if debug:
        print(to_print, end='')
    return printable


def dprint(printable: str, map: bool = True) -> str:
    """Get a printable string on a single line."""
    if map:
        for k in _dprint_map:
            printable = printable.replace(k, _dprint_map[k])
    unstrippable = []   # display unprintable ASCII
    for c in printable:
        if ord(c) <= 31 or ord(c) >= 127 and c not in unstrippable:
            unstrippable.append(c)
    for c in unstrippable:
        printable = printable.replace(c, f'\\x{ord(c):02X}')
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


def is_hex_string(s: str) -> bool:
    """Returns True if the string consists exclusively of hexadecimal chars."""
    hex_chars = '0123456789abcdefABCDEF'
    return all(c in hex_chars for c in s)


def is_b64_string(s: str) -> bool:
    """Returns True if the string consists of valid base64 characters."""
    try:
        return base64.b64encode(base64.b64decode(s)) == s
    except Exception:
        return False


def bytearray_to_str(arr: bytearray) -> str:
    """Converts a bytearray to a readable text string."""
    s = ''
    for b in bytearray(arr):
        if chr(b) in printable:
            s += chr(b)
        else:
            s += '{0:#04x}'.format(b).replace('0x', '\\')
    return s


def bytearray_to_hex_str(arr: bytearray) -> str:
    """Converts a bytearray to a hex string."""
    return binascii.hexlify(bytearray(arr)).decode()


def bytearray_to_b64_str(arr: bytearray) -> str:
    """Converts a bytearray to a base64 string."""
    return binascii.b2a_base64(bytearray(arr)).strip().decode()


def list_available_serial_ports(
    skip: Optional[list[str]] = None,
    verbose: bool = False
    ) -> Union[list[str], list[tuple[str, Optional[str]]]]:
    """Get a list of the available serial ports.
    
    Args:
        skip (list): Optional list of port names to skip when testing validity.
            Primarily to skip port(s) already in use by the application.
        verbose (bool): If True, return list of (port, USB ID) tuples.
        
    Returns:
        List of valid port names or tuples (port, usb_id).
    """
    if (skip is not None and 
        not (isinstance(skip, list) and all(isinstance(x, str) for x in skip))):
        raise ValueError('Invalid skip list')
    skip = skip or []
    system = platform.system()
    if system == 'Linux':
        candidates = glob.glob('/dev/tty[A-Z]*')
    elif system == 'Darwin':
        candidates = glob.glob('/dev/tty.*')
    else:
        from serial.tools import list_ports
        ports = list_ports.comports()
        if verbose:
            return [(p.device, p.hwid) for p in ports if p.device not in skip]
        return [p.device for p in ports if p.device not in skip]
    available = []
    for port in candidates:
        if port in skip:
            continue
        try:
            with serial.Serial(port):
                if verbose:
                    usb_id = None
                    if system == 'Linux':
                        # Example: /sys/class/tty/ttyUSB0/device/../serial
                        syspath = (f'/sys/class/tty/{os.path.basename(port)}'
                                   '/device/../serial')
                        if os.path.exists(syspath):
                            try:
                                with open(syspath) as f:
                                    usb_id = f.read().strip()
                            except Exception:
                                pass
                    elif system == 'Darwin':
                        try:
                            result = subprocess.run(
                                ["ioreg", "-p", "IOUSB", "-l"],
                                stdout=subprocess.PIPE, 
                                stderr=subprocess.DEVNULL, 
                                text=True, 
                                timeout=1
                            )
                            if port in result.stdout:
                                usb_id = "USB"
                        except Exception:
                            pass
                    available.append((port, usb_id))
                else:
                    available.append(port)
        except (OSError, serial.SerialException):
            continue
    return available
