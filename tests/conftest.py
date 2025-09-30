"""Shared test facilities for pyatcommand."""

import logging
import os
import queue
import threading
import time
from typing import Callable, Generator, Optional, Literal
from unittest.mock import Mock, patch  # noqa: F401

import pytest
import serial

from pyatcommand import AtClient, xmodem_bytes_handler
from simulator import SerialBridge, ModemSimulator

TEARDOWN_DELAY = 0.1


class MockSerial:
    """Mock replacement for serial.Serial to simulate serial communication."""
    def __init__(self, *args, **kwargs):
        self._read_buffer = queue.Queue()
        self._response = []
        self._lock = threading.Lock()
        self.is_open = True
        self.echo = kwargs.pop('echo', True)
        self.delay = 0
        self.timeout = kwargs.get('timeout', None)
        self.baudrate = kwargs.get('baudrate', 9600)
    
    def write(self, data: bytes):
        """Simulate writing data to the serial interface."""
        time.sleep(self.delay)
        with self._lock:
            if self.echo:
                for byte in data:
                    self._read_buffer.put(byte)
            if not self._response or len(self._response) == 0:
                self._response = b'\r\nOK\r\n'
            for byte in self._response:
                self._read_buffer.put(byte)
            self._response = b''
    
    def read(self, size=1) -> bytes:
        """Simulate reading data from serial interface."""
        result = b''
        with self._lock:
            for _ in range(size):
                if not self._read_buffer.empty():
                    result += bytes([self._read_buffer.get()])
        return result
    
    def read_until(self, expected=b'\n', size=None) -> bytes:
        """Simulate serial.read_until"""
        result = bytearray()
        expected_len = len(expected)
        deadline = time.time() + self.timeout if self.timeout else None
        while True:
            if self._read_buffer.empty():
                if self.timeout:
                    if time.time() >= deadline:
                        break
                time.sleep(0.01)
                continue
            result.append(self._read_buffer.get())
            if size and len(result) >= size:
                break
            if result[-expected_len:] == expected:
                break
        return bytes(result)
    
    @property
    def in_waiting(self) -> int:
        return self._read_buffer.qsize()
    
    def flush(self):
        """Stub flushing the write queue."""
        return
    
    def close(self):
        """Simulate closing the port."""
        self.is_open = False
    
    def reset_output_buffer(self):
        """Simulate clearing the Tx buffer."""
    
    def reset_buffers(self):
        """Clear the simulated buffers"""
        with self._lock:
            while not self._read_buffer.empty():
                self._read_buffer.get()
    
    def set_response(self, data: bytes):
        with self._lock:
            self._response = data


def pytest_configure(config):
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s,[%(levelname)s],(%(name)s):%(message)s',
    )


@pytest.fixture
def log_verbose():
    """Configure environment-based verbose logging"""
    os.environ['LOG_VERBOSE'] = 'atclientdev'
    # os.environ['AT_RAW'] = 'true'


@pytest.fixture
def mock_serial() -> Generator[serial.Serial, None, None]:
    with patch('serial.Serial', new=MockSerial) as mock:
        yield mock


@pytest.fixture #(scope='session')
def bridge() -> Generator[SerialBridge, None, None]:
    """Simulated serial bridge shared across tests."""
    bridge = SerialBridge()
    bridge.start()
    yield bridge
    bridge.stop()
    time.sleep(TEARDOWN_DELAY)


@pytest.fixture
def simulator(bridge: SerialBridge) -> Generator[ModemSimulator, None, None]:
    """Fresh modem simulator for each test."""
    sim = ModemSimulator()
    sim.start(port=bridge.dce)
    yield sim
    sim.stop()
    time.sleep(TEARDOWN_DELAY)


@pytest.fixture
def cclient(bridge: SerialBridge) -> Generator[AtClient, None, None]:
    """Fresh connected AtClient for each test."""
    client = AtClient()
    client.connect(port=bridge.dte, retry_timeout=5)
    yield client
    client.disconnect()
    time.sleep(TEARDOWN_DELAY)


class XmodemClient(AtClient):
    """A class for testing XMODEM handling."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._binary_handler: Optional[Callable[[bytes], None]] = None

    def set_binary_handler(self,
                           handler: Callable[[serial.Serial, Literal['recv', 'send'], Optional[bytes]], None],
                           direction: Literal['recv', 'send'] = 'recv',
                           data: Optional[bytes] = None):
        """Assign a handler to run in data mode."""
        self._binary_handler = lambda ser: handler(ser, direction, data)
    
    def close_binary_handler(self):
        self._binary_handler = None
        
    def send_bytes_data_mode(self, data, **kwargs) -> int:
        dce = kwargs.get('dce')
        if isinstance(dce, ModemSimulator):
            dce.intermediate_pause = False
        self.set_binary_handler(xmodem_bytes_handler,
                                direction='send',
                                data=data)
        self.data_mode = True
        self._binary_handler(self._serial)
        self.data_mode = False
        self._binary_handler = None
    
    def recv_bytes_data_mode(self, **kwargs) -> bytes:
        data_callback = kwargs.get('data_callback')
        strip = kwargs.get('strip', False)
        dce = kwargs.get('dce')
        if isinstance(dce, ModemSimulator):
            dce.intermediate_pause = False
        self.set_binary_handler(xmodem_bytes_handler,
                                direction='recv')
        self.data_mode = True
        data = self._binary_handler(self._serial)
        self.data_mode = False
        self._binary_handler = None
        if strip is True:
            data = data.rstrip(b'\x1a')
        if callable(data_callback):
            data_callback(data)
        else:
            logging.warning('No callback provided for data: %r', data)


@pytest.fixture
def xclient(bridge: SerialBridge) -> Generator[XmodemClient, None, None]:
    """Connected client with xmodem data mode"""
    client = XmodemClient()
    client.connect(port=bridge.dte, retry_timeout=5)
    yield client
    client.close_binary_handler()
    client.disconnect()
