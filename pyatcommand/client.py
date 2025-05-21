"""Client module for AT commands.
"""
import atexit
import logging
import os
import threading
import time
from queue import Queue, Empty
from warnings import warn

import serial
from dotenv import load_dotenv

from .constants import AT_TIMEOUT, AtErrorCode, AtParsing
from .exception import AtCrcConfigError, AtDecodeError, AtTimeout
from .utils import AtConfig, dprint, printable_char, vlog
from .crcxmodem import validate_crc, apply_crc

load_dotenv()

VLOG_TAG = 'atclient'
AT_RAW_TX_TAG = '[RAW TX >>>] '
AT_RAW_RX_TAG = '[RAW RX <<<] '

_log = logging.getLogger(__name__)


class AtResponse:
    """A class defining a response to an AT command.
    
    Attributes:
        result (AtErrorCode): The result code.
        info (str): Information returned or empty string.
        ok (bool): Flag indicating if the result code was a success.
        crc_ok (bool): Flag indicating if CRC check passed, if supported.
    """
    def __init__(self, response: str = '', result: AtErrorCode = None):
        self.info: str = response
        self.result: AtErrorCode = result
        self.crc_ok: 'bool|None' = None
    
    @property
    def ok(self) -> bool:
        return self.result == AtErrorCode.OK


class AtClient:
    """A class for interfacing to a modem from a client device."""
    def __init__(self, **kwargs) -> None:
        """Instantiate a modem client interface.
        
        Args:
            **autoconfig (bool): Automatically detects verbose configuration
                (default True)
        """
        self._supported_baudrates = [
            9600, 115200, 57600, 38400, 19200, 4800, 2400
        ]
        self._is_debugging_raw = False
        self._config: AtConfig = AtConfig()
        self._serial: serial.Serial = None
        self._timeout: 'float|None' = kwargs.get('timeout', 0)   # serial read timeout
        self._lock = threading.Lock()
        self._response_queue = Queue()
        self._response = None
        self._unsolicited_queue = Queue()
        self._stop_event = threading.Event()
        self._listener_thread: threading.Thread = None
        self._wait_no_rx_data: float = 0.1
        self._exception_queue = Queue()
        self._crc_enable: str = ''
        self._crc_disable: str = ''
        self.auto_crc: bool = kwargs.get('auto_crc', False)
        if not isinstance(self.auto_crc, bool):
            raise ValueError('Invalid auto_crc setting')
        self._cmd_pending: str = ''
        self._command_timeout = AT_TIMEOUT
        command_timeout = kwargs.get('command_timeout')
        if command_timeout:
            self.command_timeout = command_timeout
        self._is_initialized: bool = False
        self._rx_ready = threading.Event()
        self._rx_ready.set()
        atexit.register(self.disconnect)
        # legacy backward compatibility below
        self._autoconfig = kwargs.get('autoconfig', True)
        self._rx_buffer: str = ''
        self._lcmd_pending: str = ''
        self._res_ready = False
        self._cmd_error: 'AtErrorCode|None' = None
        self.ready = threading.Event()
        self.ready.set()
        self.allow_unprintable_ascii: bool = False

    @property
    def echo(self) -> bool:
        return self._config.echo
    
    @property
    def verbose(self) -> bool:
        return self._config.verbose
    
    @property
    def quiet(self) -> bool:
        return self._config.quiet
    
    def _is_crc_cmd_valid(self, cmd: str) -> bool:
        invalid_chars = ['?', self._config.cr, self._config.lf,
                         self._config.sep]
        if (isinstance(cmd, str) and cmd.startswith('AT') and '=' in cmd and
            not any(c in cmd for c in invalid_chars)):
            return True
        return False
    
    @property
    def crc_enable(self) -> str:
        """The command to enable CRC."""
        return self._crc_enable
    
    @crc_enable.setter
    def crc_enable(self, value: str):
        if not self._is_crc_cmd_valid(value):
            raise ValueError('Invalid CRC enable string')
        self._crc_enable = value
        # convenience feature for numeric toggle
        if value.endswith('=1'):
            self.crc_disable = value.replace('=1', '=0')
        
    @property
    def crc_disable(self) -> str:
        """The command to disable CRC."""
        return self._crc_disable
    
    @crc_disable.setter
    def crc_disable(self, value: str):
        if not self._is_crc_cmd_valid(value):
            raise ValueError('Invalid CRC disable string')
        self._crc_disable = value
        
    @property
    def crc_sep(self) -> str:
        """The CRC indicator to appear after the result code."""
        return self._config.crc_sep
    
    @crc_sep.setter
    def crc_sep(self, value: str):
        invalid_chars = ['=', '?', self._config.cr, self._config.lf,
                         self._config.sep]
        if (not isinstance(value, str) or len(value) != 1 or
            value in invalid_chars):
            raise ValueError('Invalid separator')
        self._config.crc_sep = value
        
    @property
    def crc(self) -> bool:
        return self._config.crc
    
    @property
    def terminator(self) -> str:
        """The command terminator character."""
        return f'{self._config.cr}'
        
    @property
    def header(self) -> str:
        """The response header common to info and result code."""
        if self._config.verbose:
            return f'{self._config.cr}{self._config.lf}'
        return ''
    
    @property
    def trailer_info(self) -> str:
        """The trailer for information responses."""
        return f'{self._config.cr}{self._config.lf}'
    
    @property
    def trailer_result(self) -> str:
        """The trailer for the result code."""
        if self._config.verbose:
            return f'{self._config.cr}{self._config.lf}'
        return self._config.cr
    
    @property
    def cme_err(self) -> str:
        """The prefix for CME errors."""
        return '+CME ERROR:'
    
    @property
    def res_V1(self) -> 'list[str]':
        """Get the set of verbose result codes compatible with startswith."""
        CRLF = f'{self._config.cr}{self._config.lf}'
        return [ f'{CRLF}OK{CRLF}', f'{CRLF}ERROR{CRLF}' ]
    
    @property
    def res_V0(self) -> 'list[str]':
        """Get the set of non-verbose result codes."""
        return [ f'0{self._config.cr}', f'4{self._config.cr}' ]
    
    @property
    def result_codes(self) -> 'list[str]':
        return self.res_V0 + self.res_V1
    
    @property
    def command_pending(self) -> str:
        return self._cmd_pending.strip()
    
    @property
    def command_timeout(self) -> float:
        return self._command_timeout
    
    @command_timeout.setter
    def command_timeout(self, value: 'float|None'):
        if value is not None and not isinstance(value, (float, int)) or value < 0:
            raise ValueError('Invalid default command timeout')
        self._command_timeout = value
    
    def _debug_raw(self) -> bool:
        """Check if environment is configured for raw serial debug."""
        return (os.getenv('AT_RAW') and
                os.getenv('AT_RAW').lower() in ['1', 'true'])
    
    def connect(self, **kwargs) -> None:
        """Connect to a serial port AT command interface.
        
        Attempts to connect and validate response to a basic `AT` query.
        If no valid response is received, cycles through baud rates retrying
        until `retry_timeout` (default forever).
        
        Args:
            **port (str): The serial port name.
            **baudrate (int): The serial baud rate (default 9600).
            **timeout (float): The serial read timeout in seconds (default 1)
            **autobaud (bool): Set to retry different baudrates (default True)
            **retry_timeout (float): Maximum time (seconds) to retry connection
                (default 0 = forever)
            **retry_delay (float): Holdoff time between reconnect attempts
                (default 0.5 seconds)
            **echo (bool): Initialize with echo (default True)
            **verbose (bool): Initialize with verbose (default True)
            **crc (bool): Initialize with CRC, if supported (default False)
            
        Raises:
            `ConnectionError` if unable to connect.
            `ValueError` for invalid parameter settings.
        """
        port = kwargs.pop('port', os.getenv('SERIAL_PORT', '/dev/ttyUSB0'))
        autobaud = kwargs.pop('autobaud', True)
        if not isinstance(autobaud, bool):
            raise ValueError('Invalid autobaud setting')
        retry_timeout = kwargs.pop('retry_timeout', 0)
        retry_delay = kwargs.pop('retry_delay', 0.5)
        init_keys = ['echo', 'verbose', 'crc', 'ati']
        init_kwargs = {k: kwargs.pop(k) for k in init_keys if k in kwargs}
        if not isinstance(retry_timeout, (int, float)) or retry_timeout < 0:
            raise ValueError('Invalid retry_timeout')
        try:
            if 'timeout' not in kwargs:
                kwargs['timeout'] = self._timeout
            self._serial = serial.Serial(port, **kwargs)
        except serial.SerialException as err:
            raise ConnectionError('Unable to open port') from err
        attempts = 0
        start_time = time.time()
        while not self.is_connected():
            if retry_timeout and time.time() - start_time > retry_timeout:
                raise ConnectionError('Timed out trying to connect')
            attempts += 1
            _log.debug('Attempting to connect to %s at %d baud (attempt %d)',
                       port, self._serial.baudrate, attempts)
            if self._get_initial_config():
                self._listener_thread = threading.Thread(target=self._listen,
                                                         name='AtListenerThread',
                                                         daemon=True)
                self._listener_thread.start()
                if self._initialize(**init_kwargs):
                    self._stop_event.clear()
                    break
            time.sleep(retry_delay)
            if autobaud:
                idx = self._supported_baudrates.index(self._serial.baudrate) + 1
                if idx >= len(self._supported_baudrates):
                    idx = 0
                self._serial.baudrate = self._supported_baudrates[idx]
        _log.debug('Connected to %s at %d baud', port, self._serial.baudrate)
    
    def _get_initial_config(self, timeout: float = 0.5) -> bool:
        with self._lock:
            if self._serial.in_waiting > 0:
                self._serial.read(self._serial.in_waiting)
            self._serial.write(f'AT\r'.encode())
            buffer = ''
            start_time = time.time()
            while time.time() - start_time < timeout:
                if self._serial.in_waiting > 0:
                    data = self._serial.read(self._serial.in_waiting)
                    buffer += data.decode('ascii', errors='ignore')
                    if not any(res in buffer for res in self.result_codes):
                        continue
                    self._config.verbose = any(res in buffer 
                                               for res in self.res_V1)
                    self._config.echo = buffer.startswith(f'AT{self.terminator}')
                    lines = buffer.splitlines()
                    for line in lines:
                        if line.startswith(self.crc_sep):
                            self._config.crc = True
                            break
                    return True
            return False
    
    def is_connected(self) -> bool:
        """Check if the modem is responding to AT commands"""
        return self._is_initialized
        
    def disconnect(self) -> None:
        """Diconnect from the serial port"""
        if self._serial:
            self._is_initialized = False
            self._stop_event.set()
            self._listener_thread.join()
            self._serial.close()
            self._serial = None
    
    @property
    def baudrate(self) -> 'int|None':
        if self._serial is None:
            return None
        return self._serial.baudrate
    
    def _toggle_raw(self, raw: bool) -> None:
        """Toggles delimiters for streaming of received characters to stdout"""
        if self._debug_raw():
            if raw:
                if not self._is_debugging_raw:
                    print(f'{AT_RAW_RX_TAG}', end='')
                self._is_debugging_raw = True
            else:
                if self._is_debugging_raw:
                    print()
                self._is_debugging_raw = False
    
    def _initialize(self,
                    echo: bool = True,
                    verbose: bool = True,
                    crc: 'bool|None' = None,
                    **kwargs) -> bool:
        """Determine or set the initial AT configuration.
        
        Args:
            echo (bool): Echo commands if True (default E1).
            verbose (bool): Use verbose formatting if True (default V1).
            crc (bool|None): Use CRC-16-CCITT if True. Property
                `crc_enable` must be a valid command.
            **ati (bool): Log basic modem information query
        
        Returns:
            True if successful.
        
        Raises:
            `ConnectionError` if serial port not enabled or no DCE response.
            `ValueError` if CRC is not `None` but `crc_enable` is undefined.
            `AtCrcConfigError` if CRC detected but not configured.
        """
        if not self._serial:
            raise ConnectionError('Serial port not configured')
        if crc is not None and not self.crc_enable:
            raise ValueError('CRC command undefined')
        try:
            _log.debug('Initializing AT configuration')
            # first manage CRC if supported
            if self.crc_enable:
                res_crc = None
                if crc and not self.crc:
                    res_crc = self.send_command(self.crc_enable)
                elif not crc and self.crc is True:
                    res_crc = self.send_command(apply_crc(self.crc_disable,
                                                          self._config.crc_sep))
                if res_crc and not res_crc.ok:
                    _log.warning('Error %sabling CRC', 'en' if crc else 'dis')
            if echo != self.echo:
                # configure echo (enabled allows disambiguating URC from response)
                echo_cmd = f'ATE{int(echo)}'
                if self.crc:
                    echo_cmd = apply_crc(echo_cmd)
                res_echo = self.send_command(echo_cmd)
                if not res_echo or not res_echo.ok:
                    _log.warning('Error setting ATE%d', int(echo))
            if verbose != self.verbose:
                # configure verbose
                verbose_cmd = f'ATV{int(verbose)}'
                if self.crc:
                    verbose_cmd = apply_crc(verbose_cmd)
                res_verbose = self.send_command(verbose_cmd)
                if not res_verbose or not res_verbose.ok:
                    _log.warning('Error setting ATV%d', int(verbose))
            # optional verbose logging of configuration details
            if vlog(VLOG_TAG):
                dbg = '\n'.join(f'{k} = {dprint(str(v))}'
                                for k, v in vars(self._config).items())
                if self.crc_enable:
                    dbg += f'CRC enable = {self.crc_enable}'
                _log.debug('AT Config:\n%s', dbg)
            self._is_initialized = True
            # optional log device information
            if kwargs.get('ati') is True:
                ati_cmd = 'ATI' if not self.crc else apply_crc('ATI')
                res_ati = self.send_command(ati_cmd, timeout=10)
                if not res_ati or not res_ati.ok:
                    _log.warning('Error querying ATI')
                else:
                    _log.info('Modem information:\n%s', res_ati.info)
        except (serial.SerialException, UnicodeDecodeError, IOError) as err:
            _log.debug('Init failed: %s', err)
            self._is_initialized = False
        return self._is_initialized
    
    def send_command(self,
                     command: str,
                     timeout: 'float|None' = AT_TIMEOUT,
                     prefix: str = '',
                     **kwargs) -> 'AtResponse|str':
        """Send an AT command and get the response.
        
        Args:
            command (str): The AT command to send.
            timeout (float): The maximum time in seconds to wait for a response.
                `None` returns immediately and any response will be orphaned.
            prefix (str): The prefix to remove.
            **raw (bool): Return the full raw response with formatting if set.
            **rx_ready_wait (float|None): Maximum time to wait for Rx ready
        
        Raises:
            `ValueError` if command is not a valid string or timeout is invalid.
            `ConnectionError` if the receive buffer is blocked.
            `AtTimeout` if no response received within timeout.
        """
        if not isinstance(command, str) or not command:
            raise ValueError('Invalid command')
        if timeout is not None:
            if not isinstance(timeout, (float, int)) or timeout < 0:
                raise ValueError('Invalid command timeout')
        if timeout == AT_TIMEOUT and self._command_timeout != AT_TIMEOUT:
            timeout = self._command_timeout
        raw = kwargs.get('raw', False)
        rx_ready_wait = kwargs.get('rx_wait_timeout', AT_TIMEOUT)
        if not isinstance(rx_ready_wait, (float, int)):
            raise ValueError('Invalid rx_ready_wait')
        with self._lock:
            if not self._rx_ready.is_set():
                _log.debug('Waiting for RX ready')
                start_time = time.time()
                self._rx_ready.wait(rx_ready_wait)
                if time.time() - start_time > rx_ready_wait:
                    err_msg = f'RX ready timed out after {timeout} seconds'
                    _log.warning(err_msg)
                    # raise ConnectionError(err_msg)
                time.sleep(0.01)   # allow time for previous command to retrieve
            while not self._response_queue.empty():
                dequeued = self._response_queue.get_nowait()
                _log.warning('Dumped prior output: %s', dprint(dequeued))
            # self._serial.reset_output_buffer()
            if self.crc and self.auto_crc:
                command = apply_crc(command)
            self._cmd_pending = command
            if not command.endswith((self.terminator, self.trailer_info)):
                self._cmd_pending += self.terminator
            _log.debug('Sending command (timeout %0.1f): %s',
                       timeout, dprint(self._cmd_pending))
            if self._debug_raw():
                print(f'{AT_RAW_TX_TAG}{dprint(self._cmd_pending)}')
            self._serial.write(f'{self._cmd_pending}'.encode())
            self._serial.flush()
            try:
                if timeout is None:
                    _log.warning(f'{command} timeout None may orphan response')
                    return
                try:
                    response: str = self._response_queue.get(timeout=timeout)
                    if response is None:
                        exc = self._exception_queue.get_nowait()
                        if exc:
                            raise exc
                    _log.debug('Response to %s: %s',
                                command, dprint(response))
                    if raw:
                        return response
                    return self._get_at_response(response, prefix)
                except Empty:
                    err_msg = f'Command timed out: {command} ({timeout} s)'
                    _log.warning(err_msg)
                    raise AtTimeout(err_msg)
            finally:
                self._cmd_pending = ''
    
    def _get_at_response(self, response: str, prefix: str = '') -> AtResponse:
        """Convert a raw response to `AtResponse`"""
        at_response = AtResponse()
        parts = [x for x in response.strip().split(self.trailer_info) if x]
        if not self._config.verbose:
            parts += parts.pop().split(self.trailer_result)
        if self._config.crc_sep in parts[-1]:
            _ = parts.pop()   # remove CRC
            at_response.crc_ok = validate_crc(response, self._config.crc_sep)
        if not (self._cmd_pending or self._lcmd_pending):
            at_response.result = AtErrorCode.URC
            at_response.info = '\n'.join(parts)
        else:
            result = parts.pop(-1)
            if result in ['OK', '0']:
                at_response.result = AtErrorCode.OK
            else:
                err_code = AtErrorCode.ERROR
                if result.startswith(('+CME', '+CMS')):
                    prefix, info = result.split('ERROR:')
                    at_response.info = info.strip()
                    err_code = AtErrorCode.CME_ERROR
                    if result.startswith('+CMS'):
                        err_code = AtErrorCode.CMS_ERROR
                at_response.result = err_code
        if (self._cmd_pending or self._lcmd_pending) and len(parts) > 0:
            if prefix:
                if (not parts[0].startswith(prefix) and
                    any(part.startswith(prefix) for part in parts)):
                    # Unexpected pre-response data
                    while not parts[0].startswith(prefix):
                        urc = parts.pop(0)
                        self._unsolicited_queue.put(urc)
                        _log.warning('Found pre-response URC: %s', dprint(urc))
                elif not parts[0].startswith(prefix):
                    _log.warning('Prefix %s not found', prefix)
                parts[0] = parts[0].replace(prefix, '', 1).strip()
            at_response.info = '\n'.join(parts)
        return at_response
    
    def get_urc(self, timeout: 'float|None' = 0.1) -> 'str|None':
        """Retrieves an Unsolicited Result Code if present.
        
        Args:
            timeout (float): The maximum seconds to block waiting
        
        Returns:
            The URC string if present or None.
        """
        try:
            return self._unsolicited_queue.get(timeout=timeout).strip()
        except Empty:
            return None
    
    def _update_config(self, prop_name: str, detected: bool):
        """Updates the AT command configuration (E, V, Q, etc.)
        
        Args:
            prop_name (str): The configuration property e.g. `echo`.
            detected (bool): The value detected during parsing.
        
        Raises:
            `ValueError` if prop_name not recognized.
        """
        if not hasattr(self._config, prop_name):
            raise ValueError('Invalid prop_name %s', prop_name)
        if getattr(self._config, prop_name) != detected:
            abbr = { 'echo': 'E', 'verbose': 'V', 'quiet': 'Q' }
            if self.crc_enable:
                pname = self.crc_enable.split('=')[0].replace('AT', '')
                abbr['crc'] = f'{pname}='
            self._toggle_raw(False)
            if prop_name in abbr:
                _log.warning('Detected %s%d - updating config',
                            abbr[prop_name], int(detected))
                setattr(self._config, prop_name, detected)
            else:
                _log.warning('Unknown property %s', prop_name)

    def _listen(self):
        """Background thread to listen for responses/unsolicited."""
        
        cr = self._config.cr.encode()
        lf = self._config.lf.encode()
        crc_sep = self._config.crc_sep.encode()
        res_V1 = [r.encode() for r in self.res_V1]
        res_V0 = [r.encode() for r in self.res_V0]
        cmx_error_prefixes = (b'+CME ERROR:', b'+CMS ERROR:')
        
        def _at_splitlines(buf: bytes, warnings: bool = False) -> 'list[bytes]':
            """Split a buffer into lines according to AT spec.
            
            V1 has headers `<cr><lf>` and trailers `<cr><lf>`.
            
            V0 has info trailers `<cr><lf>` and result trailer `<cr>`.
            
            Inserts missing headers or trailers.
            
            Args:
                buf (bytes): The buffer of bytes read.
            
            Returns:
                A list of buffers, one for each AT response line.
            """
            header = self.header.encode()
            trailer_info = self.trailer_info.encode()
            trailer_result = self.trailer_result.encode()
            lines: 'list[bytes]' = []
            start = 0
            i = 0
            while i < len(buf):
                char = buf[i:i+1]
                if not printable_char(char[0]):
                    _log.warning('Removing invalid char 0x%02X', char[0])
                    buf.pop(i)
                    continue
                next_char = buf[i+1:i+2] if i+1 < len(buf) else None
                i += 1
                if char in (cr, lf):
                    if char == cr and next_char == lf:
                        i += 1
                    lines.append(buf[start:i])
                    start = i
            if start < len(buf):
                lines.append(buf[start:])
            if header:   # V1: iterate lines to ensure headers and trailers
                vlines = []
                i = 0
                while i < len(lines):
                    line = lines[i]
                    if line == header and i+1 < len(lines):
                        vline = line + lines[i+1]
                        i += 1
                    else:
                        vline = line
                    if (vline.endswith((trailer_info, trailer_result)) and
                        not vline.startswith(header) and
                        not _is_crc(vline)):
                        if warnings:
                            _log.warning('Fixed missing header on %s',
                                         dprint(vline.decode(errors='replace')))
                        vline = header + vline
                    vlines.append(vline)
                    i += 1
                lines = vlines
            else:   # V0: iterate lines to ensure trailers
                i = 0
                while i < len(lines):
                    line = lines[i]
                    if line.endswith(trailer_info):
                        i += 1
                        continue
                    if not line.endswith(trailer_result):
                        if (line.endswith((b'0', b'4')) or
                            line.startswith(cmx_error_prefixes)):
                            if warnings:
                                _log.warning('Fixed missing V0 trailer on %s',
                                             dprint(line.decode(errors='replace')))
                            line = line + trailer_result
                    if (not line.startswith(tuple(res_V0)) and
                        not line.startswith(cmx_error_prefixes)):
                        if line.endswith(tuple(res_V0)):
                            prev_line = line[:-2]
                            line = line[-2:]
                        else:
                            split_index = line.find(b'+')
                            prev_line = line[:split_index]
                            line = line[split_index:]
                        if warnings:
                            _log.warning('Fixed missing V0 info trailer on %s',
                                         dprint(prev_line.decode(errors='replace')))
                        lines.insert(i, prev_line + trailer_info)
                        i += 1
                    lines[i] = line
                    i += 1
            return lines
        
        def _is_response(buf: bytearray, verbose: bool = True) -> bool:
            lines = _at_splitlines(buf)
            if not lines:
                return False
            last = lines[-1]
            if verbose:
                result = (any(last == res for res in res_V1) or
                          (any(last.strip().startswith(cmx)
                               for cmx in cmx_error_prefixes) and
                           last.startswith(cr+lf) and last.endswith(cr+lf)))
            else:
                result = (any(last == res for res in res_V0) or
                          (any(last.strip().startswith(cmx)
                               for cmx in cmx_error_prefixes) and
                           last.endswith(cr)))
            if result and vlog(VLOG_TAG):
                _log.debug('Found %s response: %s',
                           'V1' if verbose else 'V0',
                           dprint(buf.decode(errors='replace')))
            return result
        
        def _is_crc_enable_cmd(buf: bytes) -> bool:
            return (self.crc_enable and
                    self.command_pending.startswith(self.crc_enable) and
                    'OK' in buf.decode(errors='replace'))
        
        def _is_crc_disable_cmd(buf: bytes) -> bool:
            return (self.crc_disable and
                    self.command_pending.startswith(self.crc_disable) and
                    'OK' in buf.decode(errors='replace'))
            
        def _is_crc(buf: bytes) -> bool:
            """Check if the buffer is a CRC for a response."""
            candidate = buf.decode(errors='ignore').strip()[-5:]
            return candidate.startswith(self.crc_sep) and len(candidate) == 5
            
        def _has_echo(buf: bytes) -> bool:
            """Check if the buffer includes an echo for the pending command."""
            return self._cmd_pending and self._cmd_pending.encode() in buf
        
        def _remove_echo(buf: bytearray) -> bytearray:
            """Remove the pending command echo from the response."""
            cmd = self._cmd_pending.encode()
            if cmd in buf:
                idx = buf.find(cmd)
                if idx > 0:
                    pre_echo = buf[:idx]
                    _log.warning('Found pre-echo data: %s',
                                 dprint(pre_echo.decode(errors="replace")))
                    residual = _process_urcs(pre_echo)
                    if residual:
                        _log.warning('Dumped residual data: %s',
                                     dprint(residual.decode(errors="replace")))
                self._update_config('echo', True)
                buf = buf.replace(cmd, b'', 1)
                if vlog(VLOG_TAG):
                    _log.debug('Removed echo: %s',
                               dprint(cmd.decode(errors='replace')))
            return buf
        
        def _process_urcs(buf: bytearray) -> bytearray:
            """Process URC(s) from the buffer into the unsolicited queue."""
            lines = _at_splitlines(buf)
            for line in lines:
                if not line.strip() or _has_echo(line):
                    continue
                if _is_response(line, self.verbose):
                    _log.warning('Discarding orphan response: %s',
                                 dprint(line.decode(errors='backslashreplace')))
                else:
                    try:
                        urc = line.decode()
                    except UnicodeDecodeError:
                        _log.warning('Invalid characters in URC: %s',
                                     dprint(line.decode(errors='backslashreplace')))
                        urc = line.decode(errors='ignore')
                    self._unsolicited_queue.put(urc)
                    if vlog(VLOG_TAG):
                        _log.debug('Processed URC: %s', dprint(urc))
                buf = buf.replace(line, b'', 1)
            return buf
            
        def _complete_parsing(buf: bytearray) -> bytearray:
            """Complete the parsing of a response or unsolicited"""
            self._toggle_raw(False)
            lines = _at_splitlines(buf, warnings=True)
            buf = bytearray().join(lines)
            if (self._cmd_pending and
                (_is_response(buf, self.verbose) or _is_crc(buf))):
                try:
                    response = buf.decode()
                except UnicodeDecodeError:
                    _log.warning('Invalid characters found in response: %s',
                                 dprint(buf.decode(errors='backslashreplace')))
                    response = buf.decode(errors='ignore')
                self._response_queue.put(response)
                if vlog(VLOG_TAG):
                    _log.debug('Processed response: %s', dprint(response))
                buf = bytearray()
            else:
                buf = _process_urcs(buf)
                if buf:
                    _log.warning('Residual buffer data: %s',
                                 dprint(buf.decode(errors='backslashreplace')))
            if self._serial.in_waiting > 0:
                _log.debug('More RX data to process')
            else:
                self._rx_ready.set()
                if vlog(VLOG_TAG):
                    _log.debug('RX ready')
            return buf
        
        buffer = bytearray()
        peeked = bytearray()
        while not self._stop_event.is_set():
            if not self._serial or not self.ready.is_set():
                continue
            try:
                while self._serial.in_waiting > 0 or peeked:
                    if self._rx_ready.is_set():
                        self._rx_ready.clear()
                        if vlog(VLOG_TAG):
                            _log.debug('RX busy')
                    if not self._is_debugging_raw:
                        self._toggle_raw(True)
                    read_until = cr
                    if self.verbose:
                        read_until += lf
                    chunk = peeked or self._serial.read_until(read_until)
                    peeked = bytearray()
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    if not buffer.strip():
                        continue   # keep reading data
                    last_char = buffer[-1:]
                    if last_char == lf:
                        if vlog(VLOG_TAG + 'dev'):
                            self._toggle_raw(False)
                            _log.debug('Assessing LF: %s',
                                       dprint(buffer.decode(errors='replace')))
                        if _is_response(buffer, verbose=True):
                            self._update_config('verbose', True)
                            if _has_echo(buffer):
                                self._update_config('echo', True)
                                buffer = _remove_echo(buffer)
                            if _is_crc_enable_cmd(buffer):
                                self._update_config('crc', True)
                            if self.crc:
                                if not _is_crc_disable_cmd(buffer):
                                    if vlog(VLOG_TAG + 'dev'):
                                        _log.debug('Continue reading for CRC')
                                    continue   # keep processing for CRC
                                self._update_config('crc', False)
                            else:   # check if CRC is configured but unknown
                                peeked = self._serial.read(1)
                                if peeked == crc_sep:
                                    self._update_config('crc', True)
                                    continue
                            buffer = _complete_parsing(buffer)
                        elif _is_crc(buffer):
                            self._update_config('crc', True)
                            if _has_echo(buffer):
                                self._update_config('echo', True)
                                buffer = _remove_echo(buffer)
                            if not validate_crc(buffer.decode(errors='ignore'),
                                                self._config.crc_sep):
                                self._toggle_raw(False)
                                _log.warning('Invalid CRC')
                            buffer = _complete_parsing(buffer)
                        elif not self._cmd_pending:
                            buffer = _complete_parsing(buffer)
                    elif last_char == cr:
                        if vlog(VLOG_TAG + 'dev'):
                            self._toggle_raw(False)
                            _log.debug('Assessing CR: %s',
                                       dprint(buffer.decode(errors='replace')))
                        if _has_echo(buffer):
                            if not buffer.startswith(self.command_pending.encode()):
                                _log.debug('Assessing pre-echo URC race: %s',
                                           dprint(buffer.decode(errors='replace')))
                                buffer = _process_urcs(buffer)
                            self._update_config('echo', True)
                            buffer = _remove_echo(buffer)
                        elif _is_response(buffer, verbose=False): # check for V0
                            peeked = self._serial.read(1)
                            if peeked != lf:   # V0 confirmed
                                self._update_config('verbose', False)
                                if peeked == crc_sep:
                                    self._update_config('crc', True)
                                else:
                                    buffer = _complete_parsing(buffer)
                                    continue
            except (AtDecodeError, serial.SerialException) as err:
                buffer.clear()
                _log.error('%s: %s', err.__class__.__name__, str(err))
                self._exception_queue.put(err)
                if self._cmd_pending:
                    self._response_queue.put(None)
                if isinstance(err, serial.SerialException):
                    self._stop_event.set()
            time.sleep(self._wait_no_rx_data)   # Prevent CPU overuse
            if not self._rx_ready.is_set():
                if vlog(VLOG_TAG):
                    _log.warning('Set RX ready after no data for %0.2fs',
                                 self._wait_no_rx_data)
                    self._rx_ready.set()

    # Legacy interface below
    
    def send_at_command(self,
                        at_command: str,
                        timeout: float = AT_TIMEOUT,
                        **kwargs) -> AtErrorCode:
        """Send an AT command and parse the response
        
        Call `get_response()` next to retrieve information responses.
        Backward compatible for legacy integrations.
        
        Args:
            at_command (str): The command to send
            timeout (float): The maximum time to wait for a response.
        
        Returns:
            `AtErrorCode` indicating success (0) or failure
        """
        response = self.send_command(at_command, timeout, raw=True)
        if not response:
            if timeout is not None:
                self._cmd_error = AtErrorCode.ERR_TIMEOUT
            else:
                self._cmd_error = AtErrorCode.PENDING
        else:
            with self._lock:
                self.ready.clear()   # pause reading temporarily
                self._lcmd_pending = at_command
                at_response = self._get_at_response(response)
                if at_response.info:
                    reconstruct = at_response.info.replace('\n', '\r\n')
                    advanced_errors = [
                        AtErrorCode.CME_ERROR,
                        AtErrorCode.CMS_ERROR,
                    ]
                    if at_response.result in advanced_errors:
                        prefix = at_response.result.name.replace('_', ' ')
                        reconstruct = f'+{prefix}: ' + reconstruct
                    self._rx_buffer = f'\r\n{reconstruct}\r\n'
                self._cmd_error = at_response.result
                self._res_ready = len(at_response.info) > 0
                if not self._res_ready:
                    self._lcmd_pending = ''
                self.ready.set()   # re-enable reading
        return self._cmd_error
    
    def check_urc(self, **kwargs) -> bool:
        """Check for an unsolicited result code.
        
        Call `get_response()` next to retrieve the code if present.
        Backward compatible for legacy integrations.
        
        Returns:
            `True` if a URC was found.
        """
        if self._unsolicited_queue.qsize() == 0:
            return False
        if self._res_ready:
            return True
        try:
            self._rx_buffer = self._unsolicited_queue.get(block=False)
            self._res_ready = True
            return True
        except Empty:
            _log.error('Unexpected error getting unsolicited from queue')
        return False

    def get_response(self, prefix: str = '', clean: bool = True) -> str:
        """Retrieve the response (or URC) from the Rx buffer and clear it.
        
        Backward compatible for legacy integrations.
        
        Args:
            prefix: If specified removes the first instance of the string
            clean: If False include all non-printable characters
        
        Returns:
            Information response or URC from the buffer.
        """
        res = self._rx_buffer
        if prefix:
            if not res.strip().startswith(prefix) and prefix in res:
                lines = [l.strip() for l in res.split(self.trailer_result) if l]
                while not lines[0].startswith(prefix):
                    urc = f'{self.header}{lines.pop(0)}{self.trailer_result}'
                    self._unsolicited_queue.put(urc)
                    _log.warning('Found pre-response URC: %s', dprint(urc))
                res = f'{self.header}{(self.trailer_info).join(lines)}{self.trailer_result}'
            elif not res.strip().startswith(prefix):
                _log.warning('Prefix %s not found', prefix)
            res = res.replace(prefix, '', 1)
            if vlog(VLOG_TAG):
                _log.debug('Removed prefix (%s): %s',
                           dprint(prefix), dprint(res))
        if clean:
            res = res.strip().replace('\r\n', '\n')
            if res.startswith(('+CME', '+CMS')):
                res = res.split(': ', 1)[1]
        self._rx_buffer = ''
        if self._lcmd_pending:
            self._lcmd_pending = ''
        self._res_ready = False
        return res
    
    def is_response_ready(self) -> bool:
        """Check if a response is waiting to be retrieved.
        
        Backward compatible for legacy integrations.
        """
        return self._res_ready
    
    def last_error_code(self, clear: bool = False) -> 'AtErrorCode|None':
        """Get the last error code.
        
        Backward compatible for legacy integrations.
        """
        tmp = self._cmd_error
        if clear:
            self._cmd_error = None
        return tmp
