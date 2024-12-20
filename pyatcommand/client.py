"""Client module for AT commands.
"""
import atexit
import logging
import os
import re
import threading
import time
from queue import Queue, Empty

import serial
from dotenv import load_dotenv

from .constants import AT_TIMEOUT, AtErrorCode, AtParsing
from .utils import AtConfig, dprint, printable_char, vlog
from .crcxmodem import validate_crc, apply_crc

load_dotenv()

VLOG_TAG = 'atclient'
AT_RAW_TX_TAG = '[RAW TX >>>] '
AT_RAW_RX_TAG = '[RAW RX <<<] '

_log = logging.getLogger(__name__)


class AtResponse:
    """"""
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
        self._ignore_unprintable = True
        self._crc_enable: str = ''
        self._crc_disable: str = ''
        self.auto_crc: bool = kwargs.get('auto_crc', False)
        if not isinstance(self.auto_crc, bool):
            raise ValueError('Invalid auto_crc setting')
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
        self._rx_buffer = ''
        self._cmd_pending = ''
        self._res_parsing: AtParsing = AtParsing.NONE
        self._res_ready = False
        self._cmd_error: 'AtErrorCode|None' = None
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
            if self._try_at_command():
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
    
    def _try_at_command(self) -> bool:
        self._serial.write(f'AT{self.terminator}'.encode())
        time.sleep(0.1)
        data = self._serial.read(self._serial.in_waiting)
        response = data.decode('ascii', errors='ignore')
        return any(x in response for x in ['OK\r\n', 'ERROR\r\n', '0\r', '4\r'])
    
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
            IOError if serial port is not enabled.
            ValueError if CRC is not `None` but `crc_enable` is undefined.
        """
        if not self._serial:
            raise IOError('Serial port not configured')
        if crc is not None and not self.crc_enable:
            raise ValueError('CRC command undefined')
        try:
            _log.debug('Initializing modem')
            # derive base config from basic AT response
            res_at: AtResponse = self.send_command('AT')
            if res_at is None:
                raise IOError('DCE not responding - check connection')
            if res_at.crc_ok is not None:
                if not self.crc_enable:
                    raise IOError('CRC error with no CRC command defined')
                self._update_config('crc', True)
            # first deal with CRC if supported
            if self.crc_enable:
                res_crc = None
                if crc and not self.crc:
                    res_crc = self.send_command(self.crc_enable)
                elif not crc and self.crc is True:
                    res_crc = self.send_command(apply_crc(self.crc_disable,
                                                          self._config.crc_sep))
                if res_crc and not res_crc.ok:
                    _log.warning('Error %sabling CRC', 'en' if crc else 'dis')
            # configure echo (enabled allows disambiguating URC from response)
            echo_cmd = f'ATE{int(echo)}'
            if self.crc:
                echo_cmd = apply_crc(echo_cmd)
            res_echo = self.send_command(echo_cmd)
            if not res_echo or not res_echo.ok:
                _log.warning('Error setting ATE%d', int(echo))
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
            timeout (float): The time in seconds to wait for a response.
            prefix (str): The prefix to remove.
            **raw (bool): Return the full raw response with formatting if set.
        """
        if not isinstance(command, str) or not command:
            raise ValueError('Invalid command')
        if timeout is not None:
            if not isinstance(timeout, (float, int)) or timeout < 0:
                raise ValueError('Invalid command timeout')
        if timeout == AT_TIMEOUT and self._command_timeout != AT_TIMEOUT:
            timeout = self._command_timeout
        raw = kwargs.get('raw', False)
        with self._lock:
            if vlog(VLOG_TAG) and not self._rx_ready.is_set():
                _log.debug('Waiting for RX ready')
            start_time = time.time()
            self._rx_ready.wait(timeout)
            if time.time() - start_time > timeout:
                raise IOError('Rx wait timed out after %0.1fs', timeout)
            while not self._response_queue.empty():
                dequeued = self._response_queue.get_nowait()
                _log.warning('Dumped response: %s', dprint(dequeued))
            # self._serial.reset_output_buffer()
            if self.crc and self.auto_crc:
                command = apply_crc(command)
            self._cmd_pending = command + self.terminator
            self._res_parsing = AtParsing.RESPONSE
            if self._config.echo:
                self._res_parsing = AtParsing.ECHO
            _log.debug('Sending command (timeout %0.1f): %s',
                       timeout, dprint(self._cmd_pending))
            if self._debug_raw():
                print(f'{AT_RAW_TX_TAG}{dprint(self._cmd_pending)}')
            self._serial.write(f'{self._cmd_pending}'.encode())
            self._serial.flush()
            if timeout is None:
                return None
            try:
                response: str = self._response_queue.get(timeout=timeout)
                _log.debug('Response to %s: %s', command, dprint(response))
                if raw:
                    return response
                return self._get_at_response(response, prefix)
            except Empty:
                _log.warning('Command response timeout for: %s', command)
                return None
            finally:
                self._cmd_pending = ''
    
    def _get_at_response(self,
                         response: str,
                         prefix: str = '',
                         queried: bool = False) -> AtResponse:
        """Convert a raw response to `AtResponse`"""
        at_response = AtResponse()
        parts = [x for x in response.strip().split(self.trailer_info) if x]
        if not self._config.verbose:
            parts += parts.pop().split(self.trailer_result)
        if self._config.crc_sep in parts[-1]:
            _ = parts.pop()   # remove CRC
            at_response.crc_ok = validate_crc(response, self._config.crc_sep)
        if not self._cmd_pending and not queried:
            at_response.result = AtErrorCode.URC
            at_response.info = '\n'.join(parts)
        else:
            result = parts.pop(-1)
            if result in ['OK', '0']:
                at_response.result = AtErrorCode.OK
            else:
                at_response.result = AtErrorCode.ERROR
        if self._cmd_pending and len(parts) > 0:
            if prefix and parts[0].startswith(prefix):
                parts[0] = parts[0].replace(prefix, '').strip()
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
    
    def _read_chunk(self, size: int = None) -> str:
        """Attempt to read a character from the serial port.
        
        Args:
            size (int): The number of bytes to read. If None, read all waiting.
        
        Returns:
            str: The ASCII character
        
        Raises:
            `UnicodeDecodeError` if not printable.
        """
        if size is not None and not isinstance(size, int):
            raise ValueError('Invalid size')
        chunk = ''
        if self._serial.in_waiting > 0:
            if size is None:
                size = self._serial.in_waiting
            data = self._serial.read(size)
            if vlog(VLOG_TAG + 'dev'):
                _log.debug('Read %d-byte chunk', len(data))
            if any(not printable_char(c, self._is_debugging_raw) for c in data):
                raise UnicodeDecodeError('ascii', data, 0, len(data),
                                         'Unprintable character')
            chunk = data.decode('ascii')
        return chunk
    
    def _update_config(self, prop_name: str, detected: bool):
        """Updates the AT command configuration (E, V, Q, etc.)
        
        Args:
            prop_name (str): The configuration property e.g. `echo`.
            detected (bool): The value detected during parsing.
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
        
        def is_response(line: str, verbose: bool = True) -> bool:
            lines = [l.strip() for l in line.split('\n') if l.strip()]
            if len(lines) == 0:
                return False
            last_word = lines[-1]
            responses_V0 = ['0', '4']
            responses_V1 = ['OK', 'ERROR', '+CME ERROR', '+CMS ERROR']
            if verbose:
                return any(last_word.startswith(v1) for v1 in responses_V1)
            return any(last_word == v0 for v0 in responses_V0)
        
        def is_cmd_crc_enable() -> bool:
            return (self.crc_enable and
                    self.command_pending.startswith(self.crc_enable))
        
        def is_cmd_crc_disable() -> bool:
            return (self.crc_disable and
                    self.command_pending.startswith(self.crc_disable))
            
        def is_crc(line: str) -> bool:
            return len(line) > 4 and line[-5] == self._config.crc_sep
            
        def has_echo(buf: str) -> bool:
            return self._cmd_pending and self._cmd_pending in buf
        
        def remove_echo(buf: str) -> str:
            if self._cmd_pending and buf.startswith(self._cmd_pending):
                self._update_config('echo', True)
                buf = buf.replace(self._cmd_pending, '', 1)
                if vlog(VLOG_TAG):
                    _log.debug('Removed echo from buffer: %s', dprint(buf))
            return buf
        
        def process_urcs(buf: str) -> str:
            pattern = r'\r\n.*?\r\n'
            urcs = re.findall(pattern, buf, re.DOTALL)
            for urc in urcs:
                buf = buf.replace(urc, '', 1)
                self._unsolicited_queue.put(urc)
                _log.debug('Processed URC: %s', dprint(urc))
            return buf
            
        def complete_parsing(buf: str) -> str:
            self._toggle_raw(False)
            if self._cmd_pending:
                self._response_queue.put(buf)
            else:
                if process_urcs(buf):
                    _log.warning('Residual buffer data: %s', buf)
            if self._serial.in_waiting > 0:
                _log.debug('More RX data to process')
            else:
                self._rx_ready.set()
                if vlog(VLOG_TAG):
                    _log.debug('RX ready')
            self._res_parsing = AtParsing.NONE
            return ''
        
        buffer = ''
        peeked = ''
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
                    if peeked:
                        chunk = peeked
                        peeked = ''
                    else:
                        chunk = self._read_chunk()
                    if not chunk:
                        continue
                    if self._res_parsing == AtParsing.NONE:
                        self._res_parsing = AtParsing.RESPONSE
                    buffer += chunk
                    line = buffer.strip()
                    if not line:
                        continue
                    last_char = buffer[-1]
                    if last_char == self._config.lf:
                        if vlog(VLOG_TAG + 'dev'):
                            self._toggle_raw(False)
                            _log.debug('Assessing LF: %s', dprint(buffer))
                        if is_response(line):
                            self._update_config('verbose', True)
                            if has_echo(buffer):
                                self._update_config('echo', True)
                                buffer = remove_echo(buffer)
                            if is_cmd_crc_enable() and 'OK' in buffer:
                                self._update_config('crc', True)
                            if self.crc:
                                if not is_cmd_crc_disable():
                                    continue   # keep processing for CRC
                                if 'OK' in buffer:
                                    self._update_config('crc', False)
                            else:
                                peeked = self._read_chunk(1)
                                if peeked == self._config.crc_sep:
                                    self._update_config('crc', True)
                                    self._res_parsing = AtParsing.CRC
                                    continue
                            buffer = complete_parsing(buffer)
                        elif is_crc(line):
                            self._update_config('crc', True)
                            if has_echo(buffer):
                                self._update_config('echo', True)
                                buffer = remove_echo(buffer)
                            if not validate_crc(buffer, self._config.crc_sep):
                                self._toggle_raw(False)
                                _log.warning('Invalid CRC')
                            buffer = complete_parsing(buffer)
                        elif not self._cmd_pending:
                            buffer = complete_parsing(buffer)
                    elif last_char == self._config.cr:
                        if vlog(VLOG_TAG + 'dev'):
                            self._toggle_raw(False)
                            _log.debug('Assessing CR: %s', dprint(buffer))
                        if has_echo(buffer):
                            if not buffer.startswith(self.command_pending):
                                _log.debug('Assessing pre-echo URC race condition')
                                buffer = process_urcs(buffer)
                            self._update_config('echo', True)
                            buffer = remove_echo(buffer)
                            self._res_parsing = AtParsing.RESPONSE
                        if is_response(buffer, verbose=False): # check for V0
                            peeked = self._read_chunk(1)
                            if peeked != self._config.lf:   # V0 confirmed
                                self._update_config('verbose', False)
                                if peeked == self._config.crc_sep:
                                    self._update_config('crc', True)
                                    self._res_parsing = AtParsing.CRC
                                else:
                                    buffer = complete_parsing(buffer)
                                    continue
            except UnicodeDecodeError as err:
                _log.warning(err)
                raise
            except serial.SerialException as err:
                _log.error('Serial exception: %s', err)
                raise
            time.sleep(0.01)   # Prevent CPU overuse

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
            timeout (float): The maximum time to wait for a response
        
        Returns:
            `AtErrorCode` indicating success (0) or failure
        """
        response = self.send_command(at_command, timeout, raw=True)
        if not response:
            self._cmd_error = AtErrorCode.ERR_TIMEOUT
        else:
            with self._lock:
                self.ready.clear()   # pause reading temporarily
                self._cmd_pending = at_command
                self._rx_buffer = response
                at_response = self._get_at_response(response, queried=True)
                self._cmd_error = at_response.result
                self._res_ready = at_response.ok
                self.ready.set()   # re-enable reading
        return self._cmd_error
    
    def check_urc(self, **kwargs) -> bool:
        """Check for an unsolicited result code.
        
        Call `get_response()` next to retrieve the code if present.
        Backward compatible for legacy integrations.
        
        Returns:
            True if a URC was found.
        """
        if self._unsolicited_queue.qsize() == 0:
            return False
        try:
            self._rx_buffer = self._unsolicited_queue.get(block=False)
            return True
        except Empty:
            _log.error('Unexpected error getting unsolicited from queue')

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
        if prefix and res.strip().startswith(prefix):
            res = res.replace(prefix, '', 1)
            if vlog(VLOG_TAG):
                _log.debug('Removed prefix (%s): %s', dprint(prefix), dprint(res))
        if clean:
            res = self._get_at_response(res).info
        self._rx_buffer = ''
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
    