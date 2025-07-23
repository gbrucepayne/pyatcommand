"""XMODEM support for AT command interface.

Several cellular/satellite modems support data transfer using XMODEM protocol.
Often this involves sending an AT command that triggers a switch to data mode,
which then reverts to AT command mode after a designated number of bytes
sent/received, or a timeout.
"""

import logging
import io
from typing import Literal, Optional

import serial
from xmodem import XMODEM

_log = logging.getLogger(__name__)


def xmodem_bytes_handler(ser: serial.Serial,
                         direction: Literal['recv', 'send'],
                         data: Optional[bytes],
                         **kwargs) -> Optional[bytes]:
    getc_timeout = kwargs.get('getc_timeout', 1)
    putc_timeout = kwargs.get('putc_timeout', 1)
    getc_retry = kwargs.get('getc_retry', 16)
    log_level = logging.getLogger('xmodem').getEffectiveLevel()
    
    def getc(size: int, timeout: float = getc_timeout):
        original_timeout = ser.timeout
        ser.timeout = timeout
        data = ser.read(size)
        ser.timeout = original_timeout
        if log_level == logging.DEBUG:
            _log.debug('Read (timeout=%0.1f): %r', timeout, data)
        return data if data else None
    
    def putc(data: bytes, timeout: float = putc_timeout):
        original_timeout = ser.write_timeout
        ser.write_timeout = timeout
        ser.write(data)
        ser.flush()
        ser.write_timeout = original_timeout
        if log_level == logging.DEBUG:
            _log.debug('Write (timeout=%0.1f): %r', timeout, data)
        return len(data)
    
    xmodem = XMODEM(getc, putc)
    
    if direction == 'recv':
        _log.debug('Starting XMODEM receive...')
        buf = io.BytesIO()
        success = xmodem.recv(buf, timeout=getc_timeout, retry=getc_retry)
        if success:
            received = buf.getvalue()
            _log.debug('XMODEM receive complete: %d bytes', len(received))
            return received
        else:
            _log.error('XMODEM receive failed')
            return b''
    
    elif direction == 'send':
        if not data:
            raise ValueError('Send requires data bytes')
        buf = io.BytesIO(data)
        if xmodem.send(buf, timeout=getc_timeout, retry=getc_retry):
            _log.debug('XMODEM sent %d bytes', buf.getbuffer().nbytes)
        else:
            _log.error('XMODEM send failed')

