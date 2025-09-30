"""Client module for AT commands.
"""
import atexit
import logging
import os
import threading
import time
from queue import Empty, Queue
from typing import Any, Callable, Optional, Union

import serial
from dotenv import load_dotenv

from .common import (
    AT_TIMEOUT,
    AtConfig,
    AtErrorCode,
    AtResponse,
    dprint,
    vlog,
)
from .crcxmodem import apply_crc, validate_crc
from .exception import AtTimeout

load_dotenv()

VLOG_TAG = 'atclient'
AT_RAW_TX_TAG = '[RAW TX >>>] '
AT_RAW_RX_TAG = '[RAW RX <<<] '

_log = logging.getLogger(__name__)


class AtClient:
    """A class for interfacing to a modem from a client device."""
    
    # slots placeholder unused to allow subclasses without slots
    # __slots__ = (
    #     "_supported_baudrates",
    #     "_port",
    #     "_baudrate",
    #     "_is_debugging_raw",
    #     "_config",
    #     "_autoconfig",
    #     "_serial",
    #     "_rx_timeout",
    #     "_lock",
    #     "_listener_thread",
    #     "_rx_running",
    #     "_rx_buf",
    #     "_rx_peeked",
    #     "_response_queue",
    #     "_unsolicited_queue",
    #     "_exception_queue",
    #     "_wait_no_rx_data",
    #     "_cmd_pending",
    #     "_command_timeout",
    #     "_is_initialized",
    #     "_rx_ready",
    #     "_crc_enable",
    #     "_crc_disable",
    #     "_auto_crc",
    #     "_legacy_response",
    #     "_legacy_response_ready",
    #     "_legacy_cmd_error",
    #     "allow_unprintable_ascii",
    #     "_data_mode",
    #     "_mid_prompt",
    #     "_mid_cb",
    #     "_mid_cb_args",
    #     "_mid_cb_kwargs",
    # )
    
    def __init__(self, **kwargs) -> None:
        """Instantiate a modem client interface.
        
        Args:
            **port (str): The serial port name
            **baudrate (int): The baud rate to use (default 9600)
            **timeout (float): The serial read timeout (default 0)
            **command_timeout (float): The default time to wait for a response
                (default 0.3 seconds)
            **autoconfig (bool): Auto-detect configuration (default True)
            **auto_crc (bool): If True, apply CRC to all commands sent
        """
        self._supported_baudrates = [
            115200, 57600, 38400, 19200, 9600, 4800, 2400
        ]
        self._port: Optional[str] = kwargs.get('port', os.getenv('SERIAL_PORT'))
        self._baudrate: int = kwargs.get('baudrate',
                                         int(os.getenv('SERIAL_BAUD', '9600')))
        self._is_debugging_raw = False
        self._config: AtConfig = AtConfig()
        self._autoconfig: bool = kwargs.get('autoconfig', True)
        self._serial: Optional[serial.Serial] = None
        self._rx_timeout: Optional[float] = kwargs.get('timeout', 0.1)
        self._tx_timeout: Optional[float] = kwargs.get('write_timeout', 2)
        self._lock = threading.Lock()
        self._listener_thread: Optional[threading.Thread] = None
        self._rx_running: bool = False
        self._rx_buf = bytearray()
        self._rx_peeked = bytearray()
        self._response_queue: Queue[str] = Queue()
        self._unsolicited_queue: Queue[str] = Queue()
        self._exception_queue: Queue[Exception] = Queue()
        self._wait_no_rx_data: float = 0.1
        self._cmd_pending: str = ''
        self._command_timeout = AT_TIMEOUT
        if 'command_timeout' in kwargs:
            self.command_timeout = kwargs.get('command_timeout')
        self._is_initialized: bool = False
        self._rx_ready = threading.Event()
        atexit.register(self.disconnect)
        # Optional CRC support
        self._crc_enable: str = ''
        self._crc_disable: str = ''
        self._auto_crc: bool = kwargs.get('auto_crc', False)
        if not isinstance(self._auto_crc, bool):
            raise ValueError('Invalid auto_crc setting')
        # legacy backward compatibility below
        self._legacy_response: str = ''
        self._legacy_response_ready = False
        self._legacy_cmd_error: Optional[AtErrorCode] = None
        # Advanced debug
        self.allow_unprintable_ascii: bool = False
        # Data mode
        self._data_mode: bool = False
        self._mid_prompt: Optional[str] = None
        self._mid_cb: Optional[Callable[[], None]] = None
        self._mid_cb_args: tuple = ()
        self._mid_cb_kwargs: dict[str, Any] = {}

    @property
    def port(self) -> Optional[str]:
        return self._port
    
    @port.setter
    def port(self, value: str):
        if not isinstance(value, str) or not value:
            raise ValueError('Invalid port must be non-empty string')
        if self.is_connected():
            raise ConnectionError('Disconnect to change port')
        self._port = value
    
    @property
    def baudrate(self) -> int:
        return self._baudrate
    
    @baudrate.setter
    def baudrate(self, value: int):
        if value not in self._supported_baudrates:
            raise ValueError(f'Unsupported baudrate {value}'
                             f' must be in {self._supported_baudrates}')
        if self.is_connected():
            raise ConnectionError('Use AT command to change baudrate'
                                  ' of connected modem')
        self._baudrate = value
    
    @property
    def data_mode(self) -> bool:
        return self._data_mode
    
    @data_mode.setter
    def data_mode(self, value: bool):
        if not isinstance(value, bool):
            raise ValueError('Data mode must be boolean')
        # if value:  # complete processing of serial data first
        #     with self._lock:
        #         self._serial.flush()
        #         while self._serial.in_waiting:   # process any pending rx data
        #             time.sleep(0.1)
        _log.debug('%sing data mode', 'Enter' if value else 'Exit')
        self._data_mode = value
    
    @property
    def ready(self) -> bool:
        return (self._is_initialized and 
                self._rx_ready.is_set() and
                not self.data_mode)
    
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
        """Check if the configured CRC enable/disable command is valid."""
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
        if self.crc is None:
            self._config.crc = False
        
    @property
    def crc_disable(self) -> str:
        """The command to disable CRC."""
        return self._crc_disable
    
    @crc_disable.setter
    def crc_disable(self, value: str):
        if not self._is_crc_cmd_valid(value):
            raise ValueError('Invalid CRC disable string')
        self._crc_disable = value
        if self.crc is None:
            self._config.crc = False
        
    @property
    def crc_sep(self) -> str:
        """The CRC indicator to appear after the result code."""
        return self._config.crc_sep
    
    @crc_sep.setter
    def crc_sep(self, value: str):
        self._config.crc_sep = value
        
    @property
    def crc(self) -> bool:
        return self._config.crc
    
    @property
    def terminator(self) -> str:
        """The command terminator character(s)."""
        return self._config.terminator
    
    @terminator.setter
    def terminator(self, value: str):
        self._config.terminator = value
    
    @property
    def _cme_prefix(self) -> str:
        """The prefix for CME errors."""
        return '+CME ERROR:'
    
    @property
    def _res_V1(self) -> list[str]:
        """Get the set of verbose result codes compatible with startswith."""
        CRLF = f'{self._config.cr}{self._config.lf}'
        return [ f'{CRLF}OK{CRLF}', f'{CRLF}ERROR{CRLF}' ]
    
    @property
    def _res_V0(self) -> list[str]:
        """Get the set of non-verbose result codes."""
        return [ f'0{self._config.cr}', f'4{self._config.cr}' ]
    
    @property
    def _result_codes(self) -> list[str]:
        return self._res_V0 + self._res_V1
    
    @property
    def command_pending(self) -> str:
        return self._cmd_pending.strip()
    
    @property
    def command_timeout(self) -> float:
        return self._command_timeout
    
    @command_timeout.setter
    def command_timeout(self, value: Optional[float]):
        if value is not None and not isinstance(value, (float, int)) or value < 0:
            raise ValueError('Invalid default command timeout')
        self._command_timeout = value
    
    def connect(self, **kwargs) -> None:
        """Connect to a serial port AT command interface.
        
        Attempts to connect and validate response to a basic `AT` query.
        If no valid response is received, may cycle through baud rates retrying
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
            **crc (bool): Optional initialize with CRC, if supported (default None)
            
        Raises:
            `ConnectionError` if unable to connect.
            `ValueError` for invalid parameter settings.
        """
        self._port = kwargs.pop('port', self._port)
        if not self._port or not isinstance(self._port, str):
            raise ConnectionError('Invalid or missing serial port')
        self._baudrate = kwargs.pop('baudrate', self._baudrate)
        autobaud = kwargs.pop('autobaud', True)
        if not isinstance(autobaud, bool):
            raise ValueError('Invalid autobaud setting')
        retry_timeout = kwargs.pop('retry_timeout', 0)
        if not isinstance(retry_timeout, (int, float)) or retry_timeout < 0:
            raise ValueError('Invalid retry_timeout')
        retry_delay = kwargs.pop('retry_delay', 0.5)
        init_keys = ['echo', 'verbose', 'crc']
        init_kwargs = {k: kwargs.pop(k) for k in init_keys if k in kwargs}
        try:
            if 'timeout' not in kwargs:
                kwargs['timeout'] = self._rx_timeout
            if 'write_timeout' not in kwargs:
                kwargs['write_timeout'] = self._tx_timeout
            self._serial = serial.Serial(self._port, self._baudrate, **kwargs)
            self._rx_running = True
            self._listener_thread = threading.Thread(target=self._listen,
                                                     name='AtListenerThread',
                                                     daemon=True)
            self._rx_ready.set()
            self._listener_thread.start()
        except serial.SerialException as exc:
            raise ConnectionError(f'Unable to open {self._port}: {exc}') from exc
        attempts = 0
        start_time = time.time()
        while not self.is_connected():
            if retry_timeout and time.time() - start_time > retry_timeout:
                raise ConnectionError('Timed out trying to connect'
                                      f' on {self._port}')
            attempts += 1
            if self._initialize(**init_kwargs):
                _log.debug('Initialized AT command mode on %s at %d baud',
                           self._port, self._baudrate)
                return
            _log.debug('Failed to connect to %s at %d baud (attempt %d)',
                       self._port, self._baudrate, attempts)
            time.sleep(retry_delay)
            if autobaud:
                idx = self._supported_baudrates.index(self._serial.baudrate) + 1
                if idx >= len(self._supported_baudrates):
                    idx = 0
                self._serial.baudrate = self._supported_baudrates[idx]
                self._baudrate = self._serial.baudrate
    
    def _initialize(self, **kwargs) -> bool:
        """Determine or set the initial AT configuration.
        
        Args:
            **echo (bool): Echo commands if True (default E1).
            **verbose (bool): Use verbose formatting if True (default V1).
            **crc (bool|None): Use CRC-16-CCITT if True. Property
                `crc_enable` must be a valid command.
        
        Returns:
            True if successful.
        
        Raises:
            `ConnectionError` if serial port not enabled or no DCE response.
            `ValueError` if CRC is not `None` but `crc_enable` is undefined.
            `AtCrcConfigError` if CRC detected but not configured.
        """
        if not self._serial:
            raise ConnectionError('Serial port not configured')
        try:
            _log.debug('Initializing AT configuration %s',
                       kwargs if kwargs else '')
            _ = self.send_command('AT')
            kwargs['echo'] = kwargs.get('echo', True)
            kwargs['verbose'] = kwargs.get('verbose', True)
            for k, v in kwargs.items():
                if not isinstance(v, bool):
                    raise ValueError(f'{k} configuration must be boolean')
                # deal with CRC first since may affect subsequent commands
                if k == 'crc':
                    if not self.crc_enable:
                        raise ValueError('CRC not supported by modem')
                    if v and self.crc is False:
                        res_crc = self.send_command(self.crc_enable)
                    elif not v and self.crc is True:
                        res_crc = self.send_command(
                            apply_crc(self.crc_disable, self._config.crc_sep)
                        )
                    if not isinstance(res_crc, AtResponse) or not res_crc.ok:
                        _log.warning('Error %sabling CRC', 'en' if v else 'dis')
                # configure echo (enabled allows disambiguating URC from response)
                if k == 'echo':
                    echo_cmd = f'ATE{int(v)}'
                    if self.crc:
                        echo_cmd = apply_crc(echo_cmd)
                    res_echo = self.send_command(echo_cmd)
                    if not isinstance(res_echo, AtResponse) or not res_echo.ok:
                        _log.warning('Error setting ATE%d', int(v))
                if k == 'verbose':
                    vrbo_cmd = f'ATV{int(v)}'
                    if self.crc:
                        vrbo_cmd = apply_crc(vrbo_cmd)
                    res_vrbo = self.send_command(vrbo_cmd)
                    if not isinstance(res_vrbo, AtResponse) or not res_vrbo.ok:
                        _log.warning('Error setting ATV%d', int(v))
            # optional verbose logging of configuration details
            if vlog(VLOG_TAG):
                dbg = str(self._config)
                if self.crc_enable:
                    dbg += f'CRC enable = {self.crc_enable}'
                _log.debug('AT Config:\n%s', dbg)
            self._is_initialized = True
        except AtTimeout as exc:
            _log.debug('AT interface initialization failed: %s', exc)
            self._is_initialized = False
        return self._is_initialized
    
    def is_connected(self) -> bool:
        """Check if the modem is responding to AT commands"""
        return self._is_initialized
        
    def disconnect(self) -> None:
        """Diconnect from the serial port"""
        self._rx_running = False
        self._is_initialized = False
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=1)
            self._listener_thread = None
        if self._serial:
            self._serial.close()
            self._serial = None
    
    def send_command(self,
                     command: str,
                     timeout: Optional[float] = AT_TIMEOUT,
                     prefix: str = '',
                     **kwargs) -> AtResponse:
        """Send an AT command and get the response.
        
        The original/fixed raw response is available in `AtResponse.raw`.
        
        Data mode may be invoked by passing `mid_prompt` and
        `mid_cb` where the prompt is identified during response
        parsing to trigger the caller to set `data_mode` attribute then use the
        `send_bytes_data_mode` or `recv_bytes_data_mode` method and then
        clear `data_mode`.
        Data passed in this way should include any data mode exit sequence.
        Some implementations specify a number of bytes to expect then
        auto-revert to command mode.
        This approach expects the modem to return a final result code
        (e.g. `OK`) after exiting data mode.
        
        Args:
            command (str): The AT command to send.
            timeout (float): The maximum time in seconds to wait for a response.
                `None` returns immediately and any response will be orphaned.
            prefix (str): The prefix to remove from the information response.
            **rx_ready_wait (float|None): Maximum time to wait for Rx ready.
            **mid_prompt (str): If present, the intermediate result
                code or prompt that triggers the `mid_cb`.
                It can start or finish the response line.
            **mid_cb (Callable[...,None]): If present, triggers a
                callback function for the caller when the prompt is received.
            **mid_cb_args (tuple): If present, the arguments passed to `mid_cb`.
            **mid_cb_kwargs (dict[str, Any]): If present, the keyword arguments
                passed to `mid_cb`.
        
        Raises:
            `ValueError` if command is not a valid string or timeout is invalid.
            `ConnectionError` if the receive buffer is blocked.
            `AtTimeout` if no response received within timeout.
        """
        if not self._serial or not self._serial.is_open:
            raise ConnectionError('No serial connection')
        if not isinstance(command, str) or not command:
            raise ValueError('Invalid command')
        if self.data_mode:
            raise IOError('Cannot send command while in data mode')
        if timeout is not None:
            if not isinstance(timeout, (float, int)) or timeout < 0:
                raise ValueError('Invalid command timeout')
        if timeout == AT_TIMEOUT and self._command_timeout != AT_TIMEOUT:
            timeout = self._command_timeout
        rx_ready_wait = kwargs.get('rx_wait_timeout', AT_TIMEOUT)
        if not isinstance(rx_ready_wait, (float, int)):
            raise ValueError('Invalid rx_ready_wait')
        mid_prompt = kwargs.get('mid_prompt')
        if isinstance(mid_prompt, str):
            if not mid_prompt or mid_prompt in ['\r', '\n', '\r\n']:
                raise ValueError('Intermediate prompt must be ASCII'
                                 ' and not exclusively control characters')
            if len(mid_prompt) == 1:
                _log.debug('Intermediate prompt %s may be misinterpreted',
                           mid_prompt)
            self._mid_prompt = mid_prompt
        mid_cb = kwargs.get('mid_cb')
        if callable(mid_cb):
            if not mid_prompt:
                raise ValueError('Intermediate callback requires a prompt')
            self._mid_cb = mid_cb
            mid_cb_args = kwargs.get('mid_cb_args')
            if isinstance(mid_cb_args, tuple):
                self._mid_cb_args = mid_cb_args
            elif mid_cb_args is not None:
                raise ValueError('mid_cb_args must be a tuple')
            mid_cb_kwargs = kwargs.get('mid_cb_kwargs')
            if (isinstance(mid_cb_kwargs, dict) and
                all(isinstance(k, str) for k in mid_cb_kwargs)):
                self._mid_cb_kwargs = mid_cb_kwargs
            elif mid_cb_kwargs is not None:
                raise ValueError('mid_cb_kwargs must be a dictionary')
        with self._lock:
            full_cmd = self._prepare_command(command)
            self._rx_buf.clear()
            while not self._response_queue.empty():
                dequeued = self._response_queue.get_nowait()
                _log.warning('Dumped prior output: %s', dprint(dequeued))
            if not self._rx_ready.is_set():
                _log.debug('Waiting for RX ready')
                rx_wait_start = time.time()
                self._rx_ready.wait(rx_ready_wait)
                if time.time() - rx_wait_start >= rx_ready_wait:
                    err_msg = f'RX ready timed out after {rx_ready_wait} seconds'
                    _log.warning(err_msg)
                    # raise ConnectionError(err_msg)
                time.sleep(0.01)   # allow time for previous command to retrieve
            self._serial.reset_output_buffer()
            self._cmd_pending = full_cmd
            _log.debug('Sending command (timeout %0.1f): %s',
                       timeout, dprint(self._cmd_pending))
            if self._debug_raw():
                print(f'{AT_RAW_TX_TAG}{dprint(self._cmd_pending)}')
            try:
                self._serial.write(full_cmd.encode())
                self._serial.flush()
                start_time = time.time()
                if timeout is None:
                    _log.warning(f'{command} timeout None may orphan response')
                    return AtResponse()
                try:
                    raw: str = self._response_queue.get(timeout=timeout)
                    if raw is None:
                        exc = self._exception_queue.get_nowait()
                        if exc:
                            raise exc
                    elapsed = time.time() - start_time
                    _log.debug('Response to %s: %s', command, dprint(raw))
                    return self._get_at_response(raw, prefix, elapsed)
                except Empty:
                    err_msg = f'Command timed out: {command} ({timeout} s)'
                    _log.warning(err_msg)
                    raise AtTimeout(err_msg)
            except (serial.SerialException, OSError) as exc:
                self._handle_serial_lost(exc)
                raise            
            finally:
                self._cmd_pending = ''
                if self._mid_prompt is not None:
                    self._reset_mid_cb()
                if self._data_mode:
                    _log.debug('Auto-revert from data to command mode')
                    self._data_mode = False
    
    def _prepare_command(self, cmd: str) -> str:
        """Prepare the command before sending bytes."""
        stripped = cmd.rstrip()
        terminator = cmd[len(stripped):] or self.terminator
        if self.crc and self._auto_crc:
            cmd = apply_crc(cmd.rstrip())
        return cmd + terminator
    
    def _reset_mid_cb(self):
        """Reset the intermediate prompt settings."""
        self._mid_prompt = None
        self._mid_cb = None
        self._mid_cb_args = ()
        self._mid_cb_kwargs = {}
        
    def _get_at_response(self,
                         raw: str,
                         prefix: str = '',
                         elapsed: Optional[float] = None) -> AtResponse:
        """Convert a raw response to `AtResponse`"""
        trailer_result = self._config.trailer_result
        trailer_info = self._config.trailer_info
        at_response = AtResponse(elapsed=elapsed, raw=raw)
        parts = [x for x in raw.strip().split(trailer_info) if x]
        if not self._config.verbose:
            parts += parts.pop().split(trailer_result)
        if self._config.crc_sep in parts[-1]:
            _ = parts.pop()   # remove CRC
            at_response.crc_ok = validate_crc(raw, self._config.crc_sep)
        if not self._cmd_pending:
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
        if self._cmd_pending and len(parts) > 0:
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
    
    def send_bytes_data_mode(self, data: bytes, **kwargs) -> Union[int, None]:
        """Send bytes in a streaming mode.
        
        May be modem-specific overridden in a subclass e.g. XMODEM
        
        Args:
            data (bytes): The data to send.
            **auto (bool): If True, enables/disables data_mode around send.
            **delay (float): Optional delay after sending for multi-thread.
        
        Returns:
            The number of bytes sent or None.
            
        Raises:
            IOError if not in data mode.
            ValueError if data is not a valid bytes buffer.
        """
        if not isinstance(data, bytes):
            raise ValueError('Invalid data must be bytes/buffer')
        auto = kwargs.get('auto', False)
        if auto is True:
            self.data_mode = True
        elif not self.data_mode:
            raise IOError('Unable to stream in command mode')
        written = self._serial.write(data)
        self._serial.flush()
        time.sleep(float(kwargs.get('delay', 0)))
        if auto is True:
            self.data_mode = False
        return written
    
    def recv_bytes_data_mode(self, **kwargs) -> Union[bytes, None]:
        """Receive bytes in a streaming mode.
        
        May be modem-specific overridden in a subclass e.g. XMODEM
        
        Args:
            data (bytes): The data to send
            **timeout (float): Maximum seconds to wait for data
            **size (int): Maximum bytes to read
            **auto (bool): If True, enable/disable data_mode around receive
        
        Returns:
            The bytes read, if any. Override may return None.
            
        Raises:
            IOError if not in data mode.
        """
        auto = kwargs.get('auto', False)
        if auto is True:
            self.data_mode = True
        elif not self.data_mode:
            raise IOError('Unable to stream in command mode')
        data = bytearray()
        restore_timeout = self._serial.timeout
        temp_timeout = kwargs.get('serial_read_timeout')
        if temp_timeout:
            if not isinstance(temp_timeout, (float, int)) or temp_timeout < 0:
                raise ValueError('Timeout must be non-negative integer')
            self._serial.timeout = temp_timeout
        timeout = kwargs.get('timeout', 0)
        if not isinstance(timeout, (float, int)) or timeout < 0:
            raise ValueError('Timeout must be non-negative')
        size = kwargs.get('size')
        if size is not None and (not isinstance(size, int) or size < 1):
            raise ValueError('Size must be positive integer if specified')
        start_time = time.time()
        while not data or self._serial.in_waiting:
            read_size = size or self._serial.in_waiting or 1
            data += self._serial.read(read_size)
            if size and len(data) == size:
                _log.debug('Max data size %d bytes read', size)
                break
            if time.time() - start_time > timeout:
                _log.warning('Timed out waiting for data with %d bytes',
                             len(data))
                break
        if temp_timeout:
            self._serial.timeout = restore_timeout
        if auto is True:
            _log.debug('Auto-revert from data to command mode')
            self._data_mode = False
        return bytes(data) or None
    
    def get_urc(self, timeout: Optional[float] = 0.1) -> Optional[str]:
        """Retrieves an Unsolicited Result Code if present.
        
        Args:
            timeout (float): The maximum seconds to block waiting
        
        Returns:
            The URC string if present or None.
        """
        try:
            exc = self._exception_queue.get_nowait()
        except Empty:
            exc = None
            if exc:
                raise exc
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
        if not self._autoconfig:
            return
        if not hasattr(self._config, prop_name):
            raise ValueError('Invalid prop_name %s', prop_name)
        if getattr(self._config, prop_name) != detected:
            abbr = { 'echo': 'E', 'verbose': 'V', 'quiet': 'Q' }
            if self.crc_enable:
                pname = self.crc_enable.split('=')[0].replace('AT', '')
                abbr['crc'] = f'{pname}='
            self._toggle_rx_raw(False)
            if prop_name in abbr:
                _log.warning('Detected %s%d - updating config',
                            abbr[prop_name], int(detected))
                setattr(self._config, prop_name, detected)
            else:
                _log.warning('Unknown property %s', prop_name)

    def _handle_intermediate_callback(self, buffer: bytearray):
        """Calls back to the specified function after an intermediate result."""
        if self._mid_cb_kwargs.get('buffer') is True:
            if not isinstance(buffer, (bytearray, bytes)):
                raise ValueError('Invalid buffer')
            errors = self._mid_cb_kwargs.get('errors', 'ignore')
            self._mid_cb_kwargs['buffer'] = buffer.decode(errors=errors)
        self._mid_cb(*self._mid_cb_args, **self._mid_cb_kwargs)
        self._reset_mid_cb()
    
    def _listen(self):
        """Background thread to listen for responses/unsolicited."""
        buf = self._rx_buf
        peeked = None
        # use encoded values for bytes/bytearray
        cr = self._config.cr.encode()
        lf = self._config.lf.encode()
        crc_sep = self._config.crc_sep.encode()
        res_V1 = [r.encode() for r in self._res_V1]
        res_V0 = [r.encode() for r in self._res_V0]
        cmx_error_prefixes = (b'+CME ERROR:', b'+CMS ERROR:')
        crc_sep = self.crc_sep.encode()
        
        def _at_splitlines(buffer: bytearray, warnings: bool = False) -> list[bytes]:
            """Split a buffer into lines according to AT spec.
            
            V1 has headers `<cr><lf>` and trailers `<cr><lf>`.
            
            V0 has info trailers `<cr><lf>` and result trailer `<cr>`.
            
            Fixes lines with missing headers or trailers.
            
            Args:
                warnings (bool): If True, log any fixed lines.
            
            Returns:
                A list of buffers, one for each AT response line.
            """
            lines: list[bytes] = []
            start = 0
            i = 0
            try:
                buffer.decode()
            except UnicodeDecodeError:
                remove = buffer.decode(errors='replace').count('\uFFFD')
                _wrap_log(f'Removing {remove} invalid characters in buffer',
                          buffer, logging.WARNING)
                buffer[:] = buffer.decode(errors='ignore').encode()
            while i < len(buffer):
                char = buffer[i:i+1]
                next_char = buffer[i+1:i+2] if i+1 < len(buffer) else None
                i += 1
                if char in (cr, lf):
                    if char == cr and next_char == lf:
                        i += 1   # treat <cr><lf> as a single entry
                    lines.append(buffer[start:i])
                    start = i
            if start < len(buffer):
                lines.append(buffer[start:])
            i = 0
            while i < len(lines):
                # if <cr><lf> precedes a line then merge them
                if lines[i] == cr+lf and i + 1 < len(lines):
                    lines[i] = lines[i] + lines[i+1]
                    del lines[i+1]
                line = lines[i]
                if (line.endswith((b'OK'+cr+lf, b'ERROR'+cr+lf)) or
                    line.startswith((b'+CME ERROR:', b'+CMS ERROR:'))):
                    if not line.startswith(cr+lf):
                        lines[i] = cr+lf + line
                        if warnings:
                            _wrap_log('Fixed missing V1 result header on',
                                      line, logging.WARNING)
                elif line.endswith((b'0'+cr, b'4'+cr)) and len(line) > 2:
                    if not self._cmd_pending or not _has_echo(line):
                        lines[i] = line[:-2] + cr+lf
                        lines.insert(i+1, line[-2:])
                        if warnings:
                            _wrap_log('Fixed missing V0 info trailer on',
                                      line, logging.WARNING)
                i += 1
            return lines
        
        def _is_response(buffer: bytearray, verbose: bool = True) -> bool:
            """Check if the buffer is a command response.
            
            Args:
                buffer (bytearray): The buffer to check for response.
                verbose (bool): Check for verbose headers/trailers.
            """
            lines = _at_splitlines(buffer)
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
            return result
        
        def _is_intermediate_result(buffer: bytearray) -> bool:
            """Check if the pending command has an intermediate result.
            
            Triggers a callback if `mid_prompt`
            ends or starts the buffer.
            """
            if isinstance(self._mid_prompt, str) and len(self._mid_prompt) > 0:
                prompt = self._mid_prompt.encode()
                if prompt in buffer:
                    for line in _at_splitlines(buffer):
                        if _has_echo(line):
                            continue
                        if line.startswith(prompt) or line.endswith(prompt):
                            dbg = ('Found intermediate result'
                                   f' {dprint(self._mid_prompt)}')
                            _wrap_log(dbg, line)
                            self._mid_prompt = None
                            return True
            return False
        
        def _is_crc_enable_cmd(buffer: bytearray) -> bool:
            """Check if the pending command enables CRC."""
            return (self.crc_enable and
                    self.command_pending.startswith(self.crc_enable) and
                    'OK' in buffer.decode(errors='replace'))
        
        def _is_crc_disable_cmd(buffer: bytearray) -> bool:
            """Check if the pending command disables CRC."""
            return (self.crc_disable and
                    self.command_pending.startswith(self.crc_disable) and
                    'OK' in buffer.decode(errors='replace'))
            
        def _is_crc(buffer: bytearray) -> bool:
            """Check if the buffer is a CRC for a response."""
            lines = _at_splitlines(buffer)
            if not lines:
                return False
            last_line = lines[-1].strip()
            return last_line.startswith(crc_sep) and len(last_line) == 5
            
        def _has_echo(buffer: bytearray) -> bool:
            """Check if the buffer includes an echo for the pending command."""
            return self._cmd_pending and self._cmd_pending.encode() in buffer
        
        def _remove_echo(buffer: bytearray):
            """Remove the pending command echo from the response."""
            cmd = self._cmd_pending.encode()
            if cmd in buffer:
                idx = buf.find(cmd)
                if idx > 0:
                    pre_echo = buffer[:idx]
                    _wrap_log('Found pre-echo data', pre_echo, logging.WARNING)
                    residual = _process_urcs(pre_echo)
                    if residual:
                        _wrap_log('Dumped residual data', residual, logging.WARNING)
                    del buffer[:idx]
                del buffer[:len(cmd)]
                if vlog(VLOG_TAG):
                    _wrap_log('Removed echo', cmd)
        
        def _handle_echo(buffer: bytearray):
            """Check for and remove a command echo."""
            if _has_echo(buffer):
                self._update_config('echo', True)
                _remove_echo(buf)
            elif self.echo:
                self._update_config('echo', False)                
        
        def _process_urcs(buffer: bytearray) -> bytearray:
            """Process URC(s) from the buffer into the unsolicited queue.
            
            Args:
                buffer (bytearray): The buffer to process.
            
            Returns:
                `bytearray` of residual data in the buffer after processing.
            """
            lines = _at_splitlines(buffer)
            for line in lines:
                if not line.strip() or _has_echo(line):
                    continue
                try:
                    urc = line.decode()
                except UnicodeDecodeError:
                    _wrap_log('Invalid characters in URC', line, logging.WARNING)
                    urc = line.decode(errors='ignore')
                self._unsolicited_queue.put(urc)
                if _is_response(line, self.verbose):
                    _wrap_log('Suspected orphan response', line, logging.WARNING)
                elif vlog(VLOG_TAG):
                    _wrap_log('Processed URC', line)
                del buffer[:len(line)]
            return buffer   # residual data after parsing
            
        def _complete_parsing(buffer: bytearray) -> bool:
            """Complete the parsing of a response or unsolicited in the buffer.
            
            Args:
                buffer (bytearray): The current receive buffer
            
            Returns:
                True if there is no remaining serial data else False
            """
            lines = _at_splitlines(buffer, warnings=vlog(VLOG_TAG))
            response_lines = []
            i = 0
            found_response = False
            while i < len(lines):
                response_lines.append(lines[i])
                combined = bytearray().join(response_lines)
                if self._cmd_pending:
                    if _is_crc(combined) and self.crc:
                        if not validate_crc(combined.decode(errors='ignore'),
                                            self.crc_sep):
                            _wrap_log('Invalid CRC in response',
                                      combined, logging.WARNING)
                        found_response = True
                    elif _is_response(combined, self.verbose) and not self.crc:
                        found_response = True
                    if found_response:
                        break
                i += 1
            if found_response:
                try:
                    response = bytearray().join(response_lines).decode()
                except UnicodeDecodeError:
                    _wrap_log('Invalid characters found in response',
                              buf, logging.WARNING)
                    response = combined.decode(errors='ignore')
                self._response_queue.put(response)
                if vlog(VLOG_TAG):
                    _log.debug('Processed response: %s', dprint(response))
                # Remove consumed response lines from the list
                del lines[:i + 1]
            # Restore the buffer with remaining URCs/responses
            buffer.clear()
            buffer.extend(b''.join(lines))
            if buffer:
                residual = _process_urcs(buffer)
                if residual:
                    _wrap_log('Residual buffer data', residual, logging.WARNING)
            if self._serial.in_waiting > 0:
                _wrap_log('More RX data to process')
                return False
            else:
                self._rx_ready.set()
                if vlog(VLOG_TAG):
                    _wrap_log('RX ready')
                return True
        
        def _wrap_log(msg: str = '',
                      buffer: Optional[Union[bytearray, bytes]] = None,
                      level: int = logging.DEBUG,
                      **kwargs) -> None:
            if not isinstance(msg, str):
                raise ValueError('Invalid debug message')
            if buffer is not None and not isinstance(buffer, (bytearray, bytes)):
                raise ValueError('Invalid buffer')
            errors = kwargs.get('errors', 'backslashreplace')
            if self._debug_raw() and self._is_debugging_raw:
                self._toggle_rx_raw(False)
            if buffer is not None:
                msg += (': ' if not msg.endswith((':', ': ')) else '')
                msg += dprint(buffer.decode(errors=errors))
            _log.log(level, '%s', msg)
            
        while self._rx_running:
            try:
                while self._serial.in_waiting > 0 or peeked:
                    if self._data_mode:
                        break   # allow data mode to receive/parse
                    if self._rx_ready.is_set():
                        self._rx_ready.clear()
                        if vlog(VLOG_TAG):
                            _log.debug('RX busy')
                    read_until = cr
                    if self.verbose:
                        read_until += lf
                    chunk = peeked or self._serial.read_until(read_until)
                    if not peeked and self._debug_raw():
                        if not self._is_debugging_raw:
                            self._toggle_rx_raw(True)
                        print(dprint(chunk.decode(errors='replace')), end='')
                    peeked = None
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if not buf.strip():
                        continue   # keep reading data
                    last_char = buf[-1:]
                    
                    if last_char == lf:
                        if vlog(VLOG_TAG + 'dev'):
                            _wrap_log('Assessing LF', buf)
                        if _is_response(buf, verbose=True):
                            if vlog(VLOG_TAG):
                                _wrap_log('Found V1 response', buf)
                            self._update_config('verbose', True)
                            _handle_echo(buf)
                            if _is_crc_enable_cmd(buf):
                                self._update_config('crc', True)
                            if self.crc:
                                if not _is_crc_disable_cmd(buf):
                                    if vlog(VLOG_TAG + 'dev'):
                                        _wrap_log('Continue reading for CRC')
                                    continue   # keep processing for CRC
                                self._update_config('crc', False)
                            else:   # check if CRC is configured but unknown
                                peeked = self._serial.read(1)
                                if peeked == crc_sep:
                                    self._update_config('crc', True)
                                    continue   # keep processing for CRC
                            _complete_parsing(buf)
                        elif _is_crc(buf):
                            self._update_config('crc', True)
                            if _has_echo(buf):
                                _wrap_log('Echo should already be removed',
                                          level=logging.WARNING)
                                _handle_echo(buf)
                            _complete_parsing(buf)
                        elif not self._cmd_pending:
                            # URC(s)
                            _complete_parsing(buf)
                    
                    elif last_char == cr:
                        if vlog(VLOG_TAG + 'dev'):
                            _wrap_log('Assessing CR', buf)
                        if _is_response(buf, verbose=False): # check for V0
                            peeked = self._serial.read(1)
                            if peeked != lf:   # V0 confirmed
                                if vlog(VLOG_TAG):
                                    _wrap_log('Found V0 response', buf)
                                self._update_config('verbose', False)
                                _handle_echo(buf)
                                if peeked == crc_sep:
                                    self._update_config('crc', True)
                                else:
                                    _complete_parsing(buf)
                        assert buf is not None
                    
                    if _is_intermediate_result(buf):
                        if self._serial.in_waiting:
                            _wrap_log('Processing more intermediate data')
                            buf.extend(self._serial.read_all())
                        if callable(self._mid_cb):
                            _wrap_log('Triggering intermediate callback'
                                      f' {self._mid_cb.__name__}')
                            self._handle_intermediate_callback(buf)
                            
                time.sleep(self._wait_no_rx_data)   # Prevent CPU overuse
                if not self._rx_ready.is_set():
                    if vlog(VLOG_TAG):
                        _wrap_log('Set RX ready after no data'
                                  f' for {self._wait_no_rx_data:0.2f}s')
                    self._rx_ready.set()
        
            except (serial.SerialException, OSError) as exc:
                self._toggle_rx_raw(False)
                buf.clear()
                if self._cmd_pending:
                    self._response_queue.put(None)   # trigger send_command handling
                conn_exc = ConnectionError(*exc.args)
                self._handle_serial_lost(conn_exc)
            
        _log.debug('Lister exited')

    def _handle_serial_lost(self, exc: Optional[Exception] = None):
        """Handle a serial connection loss."""
        if exc:
            _log.error('Serial connection lost: %s %s', 
                       exc.__class__.__name__, exc)
            self._exception_queue.put(exc)
        self._rx_running = False
        self._rx_ready.set()   # ensure any waiting threads unblock
        try:
            self.disconnect()
        except Exception as inner_exc:
            _log.warning('Error disconnecting: %s', inner_exc)
    
    #--- Raw debug mode for detailed interface analysis ---#
    
    def _debug_raw(self) -> bool:
        """Check if environment is configured for raw serial debug."""
        return (os.getenv('AT_RAW') and
                os.getenv('AT_RAW').lower() in ['1', 'true'])
    
    def _toggle_rx_raw(self, raw: bool) -> None:
        """Toggles delimiters for streaming of received characters to stdout"""
        if self._debug_raw():
            if raw:
                if not self._is_debugging_raw:
                    _log.debug('Toggling raw ON')
                    print(f'{AT_RAW_RX_TAG}', end='')
                self._is_debugging_raw = True
            else:
                if self._is_debugging_raw:
                    print()
                    _log.debug('Toggled raw OFF')
                self._is_debugging_raw = False
