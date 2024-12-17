import logging
import os
import pytest
import random
import threading
import time
from unittest.mock import MagicMock, patch

from pyatcommand import AtClient, AtErrorCode
from .simulator.socat import SerialBridge, ModemSimulator, DTE, COMMAND_FILE

logger = logging.getLogger(__name__)


@pytest.fixture
def log_verbose():
    """Configure environment-based logging"""
    os.environ['LOG_VERBOSE'] = 'atclient'
    # os.environ['AT_RAW'] = 'true'


@pytest.fixture
def mock_serial():
    with patch('serial.Serial') as mock_serial_class:
        mock_instance = MagicMock()
        mock_serial_class.return_value = mock_instance
        mock_instance.baudrate = 9600
        mock_instance.delay = 0
        
        class SerialMockBuffer:
            def __init__(self, initial_buffer):
                self.buffer = None
                self.buffer_iter = None
                self.set_buffer(initial_buffer)
            
            def set_buffer(self, new_buffer):
                self.buffer = new_buffer
                self.buffer_iter = iter(self.buffer)
            
            def read(self, size):
                ba = []
                for _ in range(size):
                    b = next(self.buffer_iter, None)
                    if b is not None:
                        ba.append(b)
                return bytes(ba)
        
        serial_buffer = SerialMockBuffer(b'\r\nOK\r\n')
        
        def mock_write(data):
            if mock_instance.delay:
                logger.info('Mock delay %0.1f seconds', mock_instance.delay)
                time.sleep(mock_instance.delay)
            serial_buffer.set_buffer(b'\r\nOK\r\n')
        
        mock_instance.write.side_effect = mock_write
        mock_instance.read.side_effect = lambda size: serial_buffer.read(size)
        mock_instance.in_waiting = len(serial_buffer.buffer)
        yield mock_instance, serial_buffer


@pytest.fixture
def client():
    return AtClient()


@pytest.fixture
def bridge():
    bridge = SerialBridge()
    bridge.start()
    yield bridge
    bridge.stop()


@pytest.fixture
def simulator():
    simulator = ModemSimulator()
    simulator.start(command_file=COMMAND_FILE)
    yield simulator
    simulator.stop()


def test_connect_invalid_port(client: AtClient):
    with pytest.raises(ConnectionError):
        client.connect(port='COM99')


def test_connect_no_response(client: AtClient):
    with pytest.raises(ConnectionError):
        client.connect(retry_timeout=5)


def test_connect(log_verbose, bridge: SerialBridge, simulator: ModemSimulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5, ati=True)
    assert client.is_connected()
    client.disconnect()


def test_old_response(bridge: SerialBridge, simulator: ModemSimulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5)
    assert client.send_at_command('AT+GMI', timeout=3) == AtErrorCode.OK
    response = client.get_response()
    assert isinstance(response, str) and len(response) > 0


def test_old_response_prefix(bridge: SerialBridge, simulator: ModemSimulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5)
    assert client.send_at_command('AT+CGDCONT?', timeout=3) == AtErrorCode.OK
    response = client.get_response('+CGDCONT:')
    assert isinstance(response, str) and len(response) > 0 and '+CGDCONT' not in response


def test_old_check_urc(bridge, simulator: ModemSimulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5)
    urc = '+URC: Test'
    simulator.inject_urc(urc)
    received = False
    start_time = time.time()
    while not received and time.time() - start_time < 10:
        received = client.check_urc()
        if not received:
            time.sleep(0.1)
    if received:
        logger.info('URC latency %0.1f seconds', time.time() - start_time)
    assert received is True
    assert client.get_response() == urc

    
def test_send_command(bridge, simulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5)
    at_response = client.send_command('AT+GMI')
    assert at_response.ok
    assert isinstance(at_response.info, str) and len(at_response.info) > 0


def test_send_command_crc(bridge, simulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5)
    at_response = client.send_command('AT%CRC=1')
    assert at_response.ok
    assert at_response.crc_ok
    at_response = client.send_command('AT%CRC=0')
    assert not at_response.ok
    assert at_response.crc_ok


def test_command_prefix(bridge, simulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5)
    command = 'AT+CGDCONT?'
    prefix = '+CGDCONT:'
    at_response = client.send_command(command)
    assert len(at_response.info) > 0 and prefix in at_response.info
    at_response = client.send_command(command, prefix=prefix)
    assert at_response.ok
    assert len(at_response.info) > 0 and prefix not in at_response.info


def test_get_urc(bridge, simulator: ModemSimulator, client:AtClient):
    client.connect(port=DTE, retry_timeout=5)
    urc = '+URC: Test'
    simulator.inject_urc(urc)
    received = False
    start_time = time.time()
    while not received and time.time() - start_time < 10:
        received = client.get_urc()
        if not received:
            time.sleep(0.1)
    if received:
        logger.info('URC latency %0.1f seconds', time.time() - start_time)
    assert received == urc


def test_thread_safety(mock_serial):
    mock_serial_instance, serial_buffer = mock_serial
    interface = AtClient()
    interface.connect(port='/dev/ttyUSB99')
    
    def send_at_command(thread_id, results):
        try:
            rng = random.Random()
            mock_serial_instance.delay = rng.random() * 2
            response = interface.send_command(f'AT+TEST{thread_id}')
            results[thread_id] = response
        except Exception as e:
            results[thread_id] = f'Error: {e}'
    
    thread_count = 5
    threads = []
    results = {}
    
    for i in range(thread_count):
        thread = threading.Thread(target=send_at_command, args=(i, results))
        threads.append(thread)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for i in range(thread_count):
        assert(results[i].ok,
               f'Thread {i} failed or got unexpected response: {vars(results[i])}')
    interface.disconnect()
