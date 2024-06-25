"""Client module for AT commands.
"""
import logging
import os
import threading
import time

from serial import Serial, SerialException

from .constants import AT_TIMEOUT, AT_URC_TIMEOUT, AtErrorCode, AtParsing
from .utils import AtConfig, dprint, printable_char, vlog
from .crcxmodem import validate_crc

VLOG_TAG = 'atclient'
AT_RAW = os.getenv('AT_RAW') in [1, 'true', 'True', 'TRUE']
AT_RAW_TX_TAG = '[RAW TX >>>] '
AT_RAW_RX_TAG = '[RAW RX <<<] '

_log = logging.getLogger(__name__)


class AtClient:
    """A class for interfacing to a modem from a client device."""
    def __init__(self, autoconfig: bool = True) -> None:
        """Instantiate a modem."""
        self._autoconfig = autoconfig
        self._rx_buffer = ''
        self._cmd_pending = ''
        self._res_parsing: AtParsing = AtParsing.NONE
        self._res_ready = False
        self._cmd_error: 'AtErrorCode|None' = None
        self._debug_raw = False
        self._config: AtConfig = AtConfig()
        self._serial: Serial = None
        self.ready = threading.Event()
        self.ready.set()

    @property
    def echo(self) -> bool:
        return self._config.echo
    
    @property
    def verbose(self) -> bool:
        return self._config.verbose
    
    @property
    def quiet(self) -> bool:
        return self._config.quiet
    
    @property
    def crc(self) -> bool:
        return self._config.crc
    
    @property
    def terminator(self) -> str:
        return f'{self._config.cr}{self._config.lf}'
        
    @property
    def vres_ok(self) -> str:
        return f'{self.terminator}OK{self.terminator}'
    
    @property
    def vres_err(self) -> str:
        return f'{self.terminator}ERROR{self.terminator}'
    
    @property
    def cme_err(self) -> str:
        return f'{self.terminator}+CME ERROR:'
    
    @property
    def res_ok(self) -> str:
        return f'{AtErrorCode.OK}{self._config.cr}'
    
    @property
    def res_err(self) -> str:
        return f'{AtErrorCode.ERROR}{self._config.cr}'
    
    def connect(self, port: str, baudrate: int = 9600, **kwargs) -> None:
        """Connect to a serial port"""
        try:
            self._serial = Serial(port, baudrate, **kwargs)
        except SerialException:
            _log.error('Unable to open port %s', port)
    
    def is_connected(self) -> bool:
        """Check if the modem is responding to AT commands"""
        _log.debug('Checking connectivity...')
        valid_results = [AtErrorCode.OK, AtErrorCode.ERR_CMD_CRC]
        valid = self.send_at_command('AT') in valid_results
        self.get_response()
        return valid
        
    @property
    def baudrate(self) -> 'int|None':
        if self._serial is None:
            return None
        return self._serial.baudrate
    
    def disconnect(self) -> None:
        """Diconnect from the serial port"""
        if isinstance(self._serial, Serial):
            self._serial.close()
            self._serial = None
    
    def _read_serial_char(self, ignore_unprintable: bool = False) -> bool:
        """Read the next valid ASCII character from serial
        
        Args:
            ignore_unprintable: Flag to indicate an invalid byte was read
            
        Returns:
            True if a valid character was read or invalid ignored
        """
        if self._serial.in_waiting == 0:
            return True
        c = self._serial.read(1)[0]
        if printable_char(c, self._debug_raw):
            self._rx_buffer += chr(c)
            return True
        return ignore_unprintable
    
    def _last_char_read(self, n: int = 1) -> int:
        """Get the nth last character read"""
        if n <= 0 or len(self._rx_buffer) < n:
            return -1
        return self._rx_buffer[-n]
    
    def _toggle_raw(self, raw: bool) -> None:
        """Toggles delimiters for streaming of received characters to stdout"""
        if AT_RAW:
            if raw:
                if not self._debug_raw:
                    print(f'{AT_RAW_RX_TAG}', end='')
                self._debug_raw = True
            else:
                if self._debug_raw:
                    print()
                self._debug_raw = False
    
    def send_at_command(self,
                        at_command: str,
                        timeout: float = AT_TIMEOUT) -> AtErrorCode:
        """Send an AT command and parse the response
        
        Setting timeout 0 avoids parsing the response to allow for intermediate
        processing. The calling function must then call `_read_at_response()`.
        
        Args:
            at_command: The command to send
            timeout: The maximum time to wait for a response
        
        Returns:
            `AtErrorCode` indicating success (0) or failure
        """
        if not isinstance(self._serial, Serial) or not self._serial.is_open:
            _log.error('Serial port connection not established')
            return AtErrorCode.ERR_TIMEOUT
        self.ready.wait()
        self.ready.clear()
        self._serial.flush()
        while self._serial.in_waiting > 0:
            self._read_serial_char()
        if len(self._rx_buffer) > 0:
            _log.warning('Dumping unsolicited Rx data: %s',
                         dprint(self._rx_buffer))
            self._rx_buffer = ''
        self._cmd_pending = at_command + '\r'
        _log.debug('Sending command (timeout %0.1f): %s',
                   timeout, dprint(self._cmd_pending))
        if AT_RAW:
            print(f'{AT_RAW_TX_TAG}{dprint(self._cmd_pending)}')
        self._serial.write(self._cmd_pending.encode())
        self._serial.flush()
        if timeout != 0:
            return self._read_at_response(timeout)
        self.ready.set()
        return AtErrorCode.PENDING
    
    def _read_at_response(self, timeout: float = AT_TIMEOUT) -> AtErrorCode:
        """Read and parse individual bytes received after a command"""
        if self.ready.is_set():
           self.ready.clear() 
        if vlog(VLOG_TAG):
            _log.debug('Parsing response to %s for %d s',
                       dprint(self._cmd_pending), timeout)
        self._res_parsing = AtParsing.RESPONSE
        if self.echo:
            self._res_parsing = AtParsing.ECHO
        self._cmd_error = AtErrorCode.ERROR
        result_ok = False
        crc_found = False
        peeked: str = ''
        start_time = time.time()
        countdown = timeout
        tick = 1 if vlog(VLOG_TAG) else 0
        if vlog(VLOG_TAG):
            _log.debug('Timeout: %0.1f; Countdown: %d s', timeout, countdown)
        while (timeout == -1 or (time.time() - start_time < timeout)):
            if ((self._serial.in_waiting > 0 or peeked) and
                self._res_parsing < AtParsing.OK):
                self._toggle_raw(True)
                if peeked:
                    peeked = ''
                elif not self._read_serial_char():
                    self._cmd_error = AtErrorCode.ERR_BAD_BYTE
                    self._res_parsing = AtParsing.ERROR
                    self._toggle_raw(False)
                    _log.error('Bad byte received in response')
                    break
                last = self._last_char_read()
                if last == self._config.lf:
                    # unsolicited, V0 info-suffix/multiline sep, V1 prefix/multiline/suffix
                    if (self._res_parsing == AtParsing.ECHO or
                        self._rx_buffer.startswith(self.terminator)):
                        # check if V0 info-suffix or multiline separator
                        if self._last_char_read(2) != self._config.cr:
                            self._toggle_raw(False)
                            _log.warning('Unexpected response data removed: %s',
                                            dprint(self._rx_buffer))
                            self._rx_buffer = ''
                    if self._rx_buffer.endswith(self.vres_ok):
                        self._toggle_raw(False)
                        self._res_parsing = self._parsing_ok()
                        if self._autoconfig:
                            self._config.verbose = True
                    elif (self._rx_buffer.endswith(self.vres_err) or
                            self._rx_buffer.startswith(self.cme_err)):
                        self._toggle_raw(False)
                        self._res_parsing = self._parsing_error()
                        if self._autoconfig:
                            self._config.verbose = True
                    elif self._res_parsing == AtParsing.CRC:
                        self._toggle_raw(False)
                        if vlog(VLOG_TAG):
                            _log.debug('CRC parsing complete')
                        if not result_ok:
                            self._res_parsing = AtParsing.ERROR
                        else:
                            if validate_crc(self._rx_buffer):
                                self._res_parsing = AtParsing.OK
                            else:
                                _log.warning('Invalid CRC')
                                self._res_parsing = AtParsing.ERROR
                                self._cmd_error = AtErrorCode.ERR_CMD_CRC
                                result_ok = False
                    # else multiline separator - keep parsing
                elif last == self._config.cr:
                    if self._rx_buffer.endswith(self._cmd_pending):
                        self._toggle_raw(False)
                        if not self._rx_buffer.startswith(self._cmd_pending):
                            _log.warning('Unexpected pre-echo data removed: %s',
                                            dprint(self._rx_buffer[:len(self._cmd_pending)]))
                        if vlog(VLOG_TAG):
                            _log.debug('Echo received - clearing Rx buffer: %s',
                                        dprint(self._rx_buffer))
                        self._rx_buffer = ''
                        self._res_parsing = AtParsing.RESPONSE
                    else:
                        if self._serial.in_waiting > 0:
                            self._read_serial_char()
                            peeked = self._last_char_read()
                            if peeked == self._config.crc_sep:
                                self._toggle_raw(False)
                                self._res_parsing = self._parsing_short()
                elif (last == self._config.crc_sep and
                        self._res_parsing == AtParsing.CRC):
                    crc_found = True
                if (vlog(VLOG_TAG) and tick > 0 and len(self._rx_buffer) == 0):
                    if (time.time() - start_time) >= tick:
                        tick += 1
                        countdown -= 1
                        self._toggle_raw(False)
                        _log.debug('Countdown: %d', countdown)
            if self._res_parsing >= AtParsing.OK:
                break
        self._toggle_raw(False)
        if vlog(VLOG_TAG):
            _log.debug('Final parsing state: %s', self._res_parsing.name)
        if self._res_parsing < AtParsing.OK:
            if result_ok:
                if (self.verbose and
                    self._rx_buffer.endswith(self._config.cr)):
                    _log.info('Detected non-verbose')
                    self._config.verbose = False
                elif self.crc and not crc_found:
                    _log.info('CRC expected but not found - clearing flag')
                    self._config.crc = False
                    self._cmd_error = AtErrorCode.ERR_CRC_CONFIG
            else:
                _log.warning('AT command timeout during parsing')
                self._cmd_error = AtErrorCode.ERR_TIMEOUT
        elif self._res_parsing == AtParsing.ERROR:
            if not self.crc and crc_found:
                _log.warning('CRC detected but not expected')
                self._config.crc = True
            elif self._rx_buffer.startswith(self.cme_err):
                try:
                    self._cmd_error = (
                        int(self._rx_buffer.replace(self.cme_err, '').strip()))
                    _log.debug('Found CME ERROR code - clearing response buffer')
                except Exception as exc:
                    _log.error(exc)
                self._rx_buffer = ''
        else:
            self._res_ready = True
            self._cmd_error = AtErrorCode.OK
        if vlog(VLOG_TAG):
            _log.debug('Parsing complete (error code %d) - clearing command %s',
                       self._cmd_error, dprint(self._cmd_pending))
            if self._res_ready:
                _log.debug('Response: %s', dprint(self._rx_buffer))
        self._cmd_pending = ''
        self.ready.set()
        return self._cmd_error
    
    def _parsing_short(self) -> AtParsing:
        """Determine next stage of parsing after potential non-verbose response
        """
        if (self._rx_buffer.startswith(self.terminator) or
            not self._rx_buffer.endswith((self.res_ok, self.res_err))):
            # just read too fast, keep parsing
            return self._res_parsing
        if self._rx_buffer.endswith((self.res_ok, self.res_err)):
            # check if really response code or part of data
            rc = self.res_ok if self._rx_buffer.endswith(self.res_ok) else self.res_err
            if (self.terminator in self._rx_buffer and self._rx_buffer.split(self.terminator)[-1] != rc):
                return self._res_parsing
        if self.verbose and self._autoconfig:
            _log.warning('Clearing verbose flag due to short response: %s',
                         dprint(self._rx_buffer))
            self._config.verbose = False
        if self._rx_buffer.endswith(self.res_ok):
            return self._parsing_ok()
        return self._parsing_error()
    
    def _parsing_ok(self) -> AtParsing:
        """Determine next stage of parsing after good response"""
        if vlog(VLOG_TAG):
            _log.debug('Result OK for: %s', dprint(self._cmd_pending))
        if not self.crc:
            if 'CRC=1' in self._cmd_pending.upper() and self._autoconfig:
                _log.debug('%s enabled CRC - set flag',
                           dprint(self._cmd_pending))
                self._config.crc = True
                return AtParsing.CRC
        else:
            if ('CRC=0' in self._cmd_pending.upper() or
                'ATZ' in self._cmd_pending.upper() and
                not self._serial.in_waiting):
                _log.debug('%s disabled CRC - reset flag',
                           dprint(self._cmd_pending))
                self._config.crc = False
            else:
                return AtParsing.CRC
        return AtParsing.OK
    
    def _parsing_error(self) -> AtParsing:
        """Determine next stage of parsing after bad response"""
        _log.warning('Result ERROR for: %s', dprint(self._cmd_pending))
        if self.crc or self._serial.in_waiting > 0:
            return AtParsing.CRC
        return AtParsing.ERROR
    
    def check_urc(self,
                  read_until: str = None,
                  timeout: float = AT_URC_TIMEOUT,
                  prefix: int = '+',
                  wait: float = 0) -> bool:
        """Check for an unsolicited result code.
        
        Call `get_response()` to retrieve the code if present.
        
        Args:
            read_until: Optional terminating string (default `\r\n`)
            timeout: Maximum seconds to wait for completion if data is found
            prefix: The expected prefix for a URC (default `+`)
            wait: Optional additional seconds to wait for the prefix
        
        Returns:
            True if a URC was found.
        """
        if wait == 0 and self._serial.in_waiting == 0:
            return False
        self.ready.wait()
        self.ready.clear()
        if read_until is None:
            read_until = self.terminator
        timeout += wait
        if vlog(VLOG_TAG):
            _log.debug('Processing URC until %s or %d',
                       dprint(read_until), timeout)
        self._toggle_raw(True)
        if len(self._rx_buffer) > 0:
            _log.warning('Dumping Rx buffer: %s', dprint(self._rx_buffer))
        self._rx_buffer = ''
        urc_found = False
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            if not self._read_serial_char() and urc_found:
                self._toggle_raw(False)
                _log.warning('Bad serial byte while parsing URC')
                self._cmd_error = AtErrorCode.ERR_BAD_BYTE
                break
            if not urc_found:
                if self._last_char_read() == prefix:
                    urc_found = True
                    if (not self._rx_buffer.startswith(self.terminator) and
                        not self._rx_buffer.startswith(prefix)):
                        self._toggle_raw(False)
                        _log.warning('Dumping pre-URC data: %s',
                                     dprint(self._rx_buffer))
                        self._rx_buffer = chr(prefix)
                        self._toggle_raw(True)
            elif (len(self._rx_buffer) > len(read_until) and
                  self._rx_buffer.endswith(read_until)):
                self._res_ready = True
                break
        self._toggle_raw(False)
        if not self._res_ready:
            if len(self._rx_buffer) > 0:
                _log.warning('URC timeout no prefix and/or terminator: %s',
                             dprint(self._rx_buffer))
            self._rx_buffer = ''
        self.ready.set()
        return self._res_ready

    def _clean_response(self) -> None:
        """Clean up the Rx buffer prior to retrieval."""
        if len(self._rx_buffer) == 0:
            _log.debug('No response to clean')
            return
        if self.crc:
            crc_length = 1 + 4 + len(self.terminator)
            self._rx_buffer = self._rx_buffer[:-crc_length]
            if vlog(VLOG_TAG):
                _log.debug('Removed CRC: %s', dprint(self._rx_buffer))
        to_remove = self.vres_ok if self.verbose else self.res_ok
        self._rx_buffer = self._rx_buffer.replace(to_remove, '')
        if vlog(VLOG_TAG):
            _log.debug('Removed result code (%s): %s',
                        dprint(to_remove), dprint(self._rx_buffer))
        self._rx_buffer = self._rx_buffer.strip()
        if vlog(VLOG_TAG):
            _log.debug('Trimmed leading/trailing whitespace: %s',
                        dprint(self._rx_buffer))
        self._rx_buffer = self._rx_buffer.replace('\r\n', '\n')
        self._rx_buffer = self._rx_buffer.replace('\n\n', '\n')
        if vlog(VLOG_TAG):
            _log.debug('Consolidated line feeds: %s', dprint(self._rx_buffer))
    
    def get_response(self, prefix: str = '', clean: bool = True) -> str:
        """Retrieve the response (or URC) from the Rx buffer and clear it.
        
        Args:
            prefix: If specified removes the first instance of the string
            clean: If False include all non-printable characters
        """
        if prefix:
            self._rx_buffer.replace(prefix, '', 1)
            if vlog(VLOG_TAG):
                _log.debug('Removed prefix (%s): %s',
                            dprint(prefix), dprint(self._rx_buffer))
        if clean:
            self._clean_response()
        tmp = self._rx_buffer
        self._rx_buffer = ''
        self._res_ready = False
        return tmp
    
    def is_response_ready(self) -> bool:
        """Check if a response is waiting to be retrieved."""
        return self._res_ready
    
    def last_error_code(self, clear: bool = False) -> 'AtErrorCode|None':
        """Get the last error code"""
        tmp = self._cmd_error
        if clear:
            self._cmd_error = None
        return tmp
    