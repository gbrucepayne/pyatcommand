"""Server module for simulating a modem replying to AT commands.
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from serial import Serial

from .constants import AtErrorCode, AtParsing
from .exception import AtCommandUnknown
from .utils import AtConfig

_log = logging.getLogger(__name__)


@dataclass
class AtCommand:
    """Class defining handlers for AT command processing.
    
    Callables must accept a string of parameters or empty string
    and return a tuple[AtErrorCode|AtCmeError, info|None]
    """
    name: str
    read: 'Callable|None' = None
    run: 'Callable|None' = None
    test: 'Callable|None' = None
    write: 'Callable|None' = None

    def __post_init__(self):
        check = (self.read, self.run, self.write)
        if not any(callable(attr) for attr in check):
            raise ValueError('At least one of run, read, write must be callable')


@dataclass
class AtCmeError:
    code: 'int|None' = None
    info: str = ''


class AtServer:
    """A class defining a server/modem for AT commands."""
    def __init__(self) -> None:
        self._config: AtConfig = AtConfig()
        self._serial: 'Serial|None' = None
        self._listener: threading.Thread = None
        self._stop = threading.Event()
        self._stop.clear()
        self._lock = threading.Lock()
        self._rx_buffer: str = ''
        self._tx_buffer: str = ''
        self._parsing: AtParsing = AtParsing.NONE
        self._cme_error_level: int = 0
        self._last_error: 'AtErrorCode|str|None' = None
        self._commands: 'dict[str, AtCommand]' = {}
    
    @property
    def echo(self) -> bool:
        return self._config.echo
    
    @echo.setter
    def echo(self, value: 'bool|int'):
        if not isinstance(value, bool):
            raise ValueError('Invalid setting')
        self._config.echo = value
    
    @property
    def verbose(self) -> bool:
        return self._config.verbose
    
    @verbose.setter
    def verbose(self, value: 'bool|int'):
        if not isinstance(value, bool):
            raise ValueError('Invalid setting')
        self._config.verbose = value
    
    @property
    def cme_error(self) -> int:
        return self._cme_error_level
    
    @cme_error.setter
    def cme_error(self, value: int):
        if not isinstance(value, int) or value not in range(0, 3):
            raise ValueError('Invalid CME setting')
        self._cme_error_level = value
    
    @property
    def terminator(self) -> str:
        return self._config.cr
    
    @property
    def separator(self) -> str:
        return self._config.sep
    
    @property
    def result_header(self) -> str:
        if self.verbose:
            return f'{self._config.cr}{self._config.lf}'
        return ''
    
    @property
    def result_trailer(self) -> str:
        if self.verbose:
            return f'{self._config.cr}{self._config.lf}'
        return self._config.cr
    
    @property
    def info_header(self) -> str:
        return f'{self._config.cr}{self._config.lf}'
    
    @property
    def info_trailer(self) -> str:
        return f'{self._config.cr}{self._config.lf}'

    def _run(self):
        rx_buffer = ''
        while not self._stop.is_set():
            if not self._serial:
                continue
            while self._serial.in_waiting > 0:
                data = self._serial.read()
                if self.echo:
                    self._serial.write(data)
                try:
                    rx_buffer += data.decode()
                    while self.terminator in rx_buffer:
                        for c in rx_buffer:
                            if c in (self.terminator, self.separator):
                                command, rx_buffer = rx_buffer.split(c, 1)
                                if c == self.separator:
                                    command = f'AT{command}'
                                self._handle_command(command)
                except UnicodeDecodeError:
                    self.send_error()
            time.sleep(0.01)

    def start(self):
        """Start the AT command server."""
        if self._stop.is_set():
            self._stop.clear()
            self._last_error = None
            self._listener = threading.Thread(target=self._run,
                                              name='AtServerListenerThread',
                                              daemon=True)
            self._listener.start()
        else:
            _log.warning('AT server already started.')
    
    def stop(self, reason: str = None):
        """Stop the AT command server."""
        if not self._stop.is_set():
            dbg_msg = 'Stopping AT server'
            if isinstance(reason, str) and len(reason) > 0:
                dbg_msg += f' ({reason})'
            _log.debug(dbg_msg)
            self._stop.set()
            if self._listener:
                self._listener.join()
                self._listener = None
        else:
            _log.warning('AT server already stopped.')
    
    def _handle_command(self, command: str):
        """Process an AT command to generate a response."""
        if not self._serial:
            raise ConnectionError('No serial connection.')
        try:
            if command.upper() in ('ATE0', 'ATE1'):
                self.echo = bool(int(command[-1]))
            elif command.upper() in ('ATV0', 'ATV1'):
                self.verbose = bool(int(command[-1]))
            else:
                result_code = None
                info = None
                params = None
                if '=' in command:
                    name, params = command.split('=')
                    operation = 'write' if params != '?' else 'test'
                elif command.endswith('?'):
                    name = command.split('?')[0]
                    operation = 'read'
                else:
                    name = command
                    operation = 'run'
                for cmd, handler in self._commands.items():
                    if cmd == name:
                        if (hasattr(handler, operation) and
                            callable(handler[operation])):
                            result_code, info = handler[operation](params)
                        break
                if not result_code:
                    raise AtCommandUnknown
                if result_code == AtErrorCode.OK:
                    if info:
                        if not isinstance(info, list):
                            info = [info]
                        for line in info:
                            self.send_info(line)
                else:
                    if isinstance(result_code, AtCmeError):
                        self._last_error = result_code.code or result_code.info
                    elif info:
                        self._last_error = info
                    else:
                        self._last_error = result_code
                    self.send_error(info)
                    return
            self.send_ok()
        except (AtCommandUnknown, ValueError) as e:
            self._last_error = AtErrorCode.ERR_CMD_UNKNOWN
            if isinstance(e, ValueError):
                _log.warning('TODO: error handling for %s', e)
            self.send_error()
    
    def add_command(self, command: AtCommand, replace: bool = False) -> bool:
        """Add a command handler."""
        if not isinstance(command, AtCommand) or not command.name:
            raise ValueError('Invalid AT command definition')
        if command.name in self._commands and not replace:
            raise ValueError('Command already exists')
        # TODO: additional validation on callbacks
        self._commands[command.name] = command
    
    def get_last_error(self) -> 'AtErrorCode|str|None':
        return self._last_error
    
    def send_info(self, info: str):
        """Sends an information line to the DCE."""
        if not self._serial:
            raise ConnectionError('No serial connection')
        response = f'{self.info_header}{info}{self.info_trailer}'
        self._serial.write(response.encode())
    
    def send_ok(self):
        """Sends an OK result code to the DCE."""
        if not self._serial:
            raise ConnectionError('No serial connection')
        response = 'OK' if self.verbose else '0'
        response = f'{self.result_header}{response}{self.result_trailer}'
        self._serial.write(response.encode())
    
    def send_error(self, cme: str = None):
        """Sends an ERROR result to the DCE."""
        if not self._serial:
            raise ConnectionError('No serial connection')
        response = 'ERROR' if self.verbose else '4'
        response = f'{self.result_header}{response}{self.result_trailer}'
        if self.cme_error and isinstance(cme, str) and len(cme) > 0:
            wrapper = f'{self._config.cr}{self._config.lf}'
            response = f'{wrapper}+CME ERROR: {cme}{wrapper}'
        self._serial.write(response.encode())
