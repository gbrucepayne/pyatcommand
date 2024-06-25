"""Server module for simulating a modem replying to AT commands.
"""
import logging
import threading
from dataclasses import dataclass
from typing import Callable

from serial import Serial

from .constants import AtErrorCode, AtParsing
from .utils import AtConfig

_log = logging.getLogger(__name__)


@dataclass
class AtCommand:
    name: 'str|None' = None
    read: 'Callable|None' = None
    run: 'Callable|None' = None
    test: 'Callable|None' = None
    write: 'Callable|None' = None


class AtServer:
    def __init__(self) -> None:
        raise NotImplementedError
        self._config: AtConfig = AtConfig()
        self._serial: 'Serial|None' = None
        self._rx_buffer: str = ''
        self._tx_buffer: str = ''
        self._initialized: bool = False
        self._parsing: AtParsing = AtParsing.NONE
        self._cmd_error: 'AtErrorCode|None' = None
        self._commands: 'dict[str, AtCommand]' = {}
        self.ready = threading.Event()
        self.ready.set()
    
    def _read_serial_char(self, ignore_unprintable: bool = False) -> bool:
        """"""
        raise NotImplementedError
    
    def _last_char_read(self, n: int = 1) -> int:
        """"""
        raise NotImplementedError
    
    def _handle_command(self):
        """"""
        raise NotImplementedError
    
    def add_command(self, command: AtCommand, replace: bool = False) -> bool:
        """"""
        raise NotImplementedError
    
    def send(self, response: str, ok: bool = False, error: bool = False):
        """"""
        raise NotImplementedError
    
    def sendOk(self):
        """"""
        raise NotImplementedError
    
    def sendError(self):
        """"""
        raise NotImplementedError
