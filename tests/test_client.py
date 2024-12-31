import logging
import os
import pytest
import queue
import random
import threading
import time
from unittest.mock import Mock, patch

from serial.tools.list_ports import comports

from pyatcommand import AtClient, AtErrorCode, AtTimeout, AtCrcConfigError, AtDecodeError
from .simulator.socat import SerialBridge, ModemSimulator, DTE, COMMAND_FILE

logger = logging.getLogger(__name__)

REAL_UART = os.getenv('REAL_UART', '/dev/ttyUSB0')


@pytest.fixture
def log_verbose():
    """Configure environment-based logging"""
    os.environ['LOG_VERBOSE'] = 'atclientdev'
    # os.environ['AT_RAW'] = 'true'


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
    
    @property
    def in_waiting(self) -> int:
        return self._read_buffer.qsize()
    
    def flush(self):
        """Stub flushing the write queue."""
        return
    
    def close(self):
        """Simulate closing the port."""
        self.is_open = False
    
    def reset_buffers(self):
        """Clear the simulated buffers"""
        with self._lock:
            while not self._read_buffer.empty():
                self._read_buffer.get()
    
    def set_response(self, data: bytes):
        with self._lock:
            self._response = data


@pytest.fixture
def mock_serial():
    with patch('serial.Serial', new=MockSerial) as mock:
        yield mock


@pytest.fixture
def client():
    return AtClient()


@pytest.fixture
def cclient():
    """Connected client"""
    client = AtClient()
    client.connect(port=DTE, retry_timeout=5)
    yield client
    client.disconnect()


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
        client.connect(retry_timeout=2)


def test_connect(log_verbose, bridge: SerialBridge, simulator: ModemSimulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=2, ati=True)
    assert client.is_connected()
    client.disconnect()


def test_ati_log_output(caplog, bridge, simulator, client: AtClient):
    """"""
    caplog.set_level(logging.INFO)
    client.connect(port=DTE, ati=True)
    assert "First line" in caplog.text
    client.disconnect()


@pytest.mark.skipif(not any(port.device == REAL_UART for port in comports()),
                    reason = f'{REAL_UART} not available')
def test_autobaud(log_verbose):
    """Testing autobaud requires use of a physical serial port."""
    real_uart = REAL_UART
    unlikely_baud = 2400
    client = AtClient()
    with pytest.raises(ConnectionError):
        client.connect(port=real_uart, baudrate=unlikely_baud, retry_timeout=2, autobaud=False)
    assert client.is_connected() is False
    client.connect(port=real_uart, baudrate=unlikely_baud)
    assert client.is_connected() is True
    assert client.baudrate != unlikely_baud
    client.disconnect()


def test_legacy_response(bridge: SerialBridge, simulator: ModemSimulator, cclient: AtClient):
    assert cclient.send_at_command('AT+GMI', timeout=3) == AtErrorCode.OK
    response = cclient.get_response()
    assert response == 'Simulated Modems Inc'
    assert cclient.send_at_command('ATI') == AtErrorCode.OK
    response = cclient.get_response()
    assert response == 'First line\nSecond line'


def test_legacy_response_prefix(bridge: SerialBridge, simulator: ModemSimulator, cclient: AtClient):
    assert cclient.send_at_command('AT+CGDCONT?', timeout=3) == AtErrorCode.OK
    response = cclient.get_response('+CGDCONT:')
    assert isinstance(response, str) and len(response) > 0 and '+CGDCONT' not in response


def test_legacy_check_urc(bridge, simulator: ModemSimulator, cclient: AtClient):
    urc = '+URC: Test'
    simulator.inject_urc(urc)
    received = False
    start_time = time.time()
    while not received and time.time() - start_time < 10:
        received = cclient.check_urc()
        if not received:
            time.sleep(0.1)
    if received:
        logger.info('URC latency %0.1f seconds', time.time() - start_time)
    assert received is True
    assert cclient.get_response() == urc

    
def test_legacy_then_urc(bridge, simulator: ModemSimulator, cclient: AtClient):
    urc = '+URC: Test'
    assert cclient.send_at_command('AT+GMI', timeout=3) == AtErrorCode.OK
    assert cclient.is_response_ready() is True
    response = cclient.get_response()
    assert cclient.is_response_ready() is False
    assert response == 'Simulated Modems Inc'
    assert cclient.ready.is_set()
    simulator.inject_urc(urc)
    received = False
    start_time = time.time()
    while not received and time.time() - start_time < 10:
        received = cclient.check_urc()
        if not received:
            time.sleep(0.1)
    if received:
        logger.info('URC latency %0.1f seconds', time.time() - start_time)
    assert received is True
    assert cclient.is_response_ready() is True
    assert cclient.get_response() == urc
    assert cclient.is_response_ready() is False
    assert cclient.send_at_command('AT+GMI', timeout=3) == AtErrorCode.OK
    response = cclient.get_response()
    assert response == 'Simulated Modems Inc'
    # send command without information response
    assert cclient.send_at_command('ATZ') == AtErrorCode.OK
    assert cclient.is_response_ready() == False
    simulator.inject_urc(urc)
    received = False
    start_time = time.time()
    while not received and time.time() - start_time < 10:
        received = cclient.check_urc()
        if not received:
            time.sleep(0.1)
    if received:
        logger.info('URC latency %0.1f seconds', time.time() - start_time)
    assert received is True
    assert cclient.is_response_ready() is True
    assert cclient.get_response() == urc
    


def test_send_command(bridge, simulator, cclient: AtClient):
    at_response = cclient.send_command('AT+GMI')
    assert at_response.ok
    assert at_response.info == 'Simulated Modems Inc'


def test_non_verbose(bridge, simulator: ModemSimulator, client: AtClient):
    """Test responses with V0"""
    client.connect(port=DTE, retry_timeout=5, verbose=False)
    at_response = client.send_command('AT+GMI')
    assert at_response.ok
    assert isinstance(at_response.info, str) and len(at_response.info) > 0
    client.disconnect()


def test_send_command_crc(bridge, simulator, cclient: AtClient):
    cclient.crc_enable = 'AT%CRC=1'
    assert cclient.crc_disable == 'AT%CRC=0'
    assert cclient.crc is False
    at_response = cclient.send_command('AT%CRC=1')
    assert at_response.ok
    assert at_response.crc_ok is True
    assert cclient.crc is True
    at_response = cclient.send_command('AT%CRC=0')
    assert not at_response.ok
    assert at_response.crc_ok
    assert cclient.crc is True
    at_response = cclient.send_command('AT%CRC=0*BBEB')
    assert at_response.ok
    assert at_response.crc_ok is None
    assert cclient.crc is False


def test_command_prefix(bridge, simulator, cclient: AtClient):
    command = 'AT+CGDCONT?'
    prefix = '+CGDCONT:'
    at_response = cclient.send_command(command)
    assert len(at_response.info) > 0 and prefix in at_response.info
    at_response = cclient.send_command(command, prefix=prefix)
    assert at_response.ok
    assert len(at_response.info) > 0 and prefix not in at_response.info


def test_get_urc(bridge, simulator: ModemSimulator, cclient:AtClient):
    urc = '+URC: Test'
    simulator.inject_urc(urc)
    received = False
    start_time = time.time()
    while not received and time.time() - start_time < 10:
        received = cclient.get_urc()
        if not received:
            time.sleep(0.1)
    if received:
        logger.info('URC latency %0.1f seconds', time.time() - start_time)
    assert received == urc


def test_multiline(bridge, simulator: ModemSimulator, cclient: AtClient):
    """Multiline responses"""
    response = cclient.send_command('ATI')
    assert response.ok and len(response.info.split('\n')) > 1


def test_multi_urc(bridge, simulator: ModemSimulator, cclient: AtClient):
    urcs = [
        '%NOTIFY:"RRCSTATE",2',
        '%NOTIFY:"RRCSTATE",0',
        '%NOTIFY:"RRCSTATE",2',
        '+CEREG: 0,,,,,,,"00111000"',
    ]
    simulator.multi_urc(urcs)
    received_count = 0
    while received_count < len(urcs):
        if cclient.get_urc():
            received_count += 1
    assert received_count == len(urcs)


def test_urc_send_race(bridge, simulator: ModemSimulator, cclient: AtClient):
    """Try to emulate a command being sent while a URC is processing."""
    long_urc = '+LONGURC: ' + 'x' * 25
    chained_urcs = [long_urc] * 3
    
    def urc_trigger():
        urcs_rcvd = 0
        simulator.multi_urc(chained_urcs)
        while urcs_rcvd < len(chained_urcs):
            if cclient.get_urc():
                urcs_rcvd += 1
        assert urcs_rcvd == len(chained_urcs)
    
    def command_trigger():
        cmd_res = cclient.send_command('AT+GDELAY?', timeout=3)
        assert cmd_res is not None and cmd_res.ok
    
    urc_thread = threading.Thread(target=urc_trigger, name='UrcTestThread', daemon=True)
    cmd_thread = threading.Thread(target=command_trigger, name='CmdTestThread', daemon=True)
    urc_thread.start()
    cmd_thread.start()
    urc_thread.join()
    cmd_thread.join()


def test_thread_safety(mock_serial):
    interface = AtClient()
    interface.connect(port='/dev/ttyUSB99')
    
    def send_at_command(thread_id, results):
        try:
            rng = random.Random()
            mock_serial.delay = rng.random() * 2
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
        assert results[i].ok, f'Thread {i} failed or got unexpected response: {vars(results[i])}'
    interface.disconnect()


def test_cme_error(bridge, simulator: ModemSimulator, cclient: AtClient):
    at_response = cclient.send_command('AT+CMEE=4')
    assert at_response.ok is False
    assert at_response.info == 'invalid configuration'


def test_legacy_cme_error(bridge, simulator: ModemSimulator, cclient: AtClient):
    assert cclient.send_at_command('AT+CMEE=4') == AtErrorCode.ERROR
    assert cclient.is_response_ready() is True
    res = cclient.get_response()
    assert res == 'invalid configuration'
    cclient.send_at_command('AT+CMEE=4')
    raw = cclient.get_response(clean=False)
    assert raw == '\r\n+CME ERROR: invalid configuration\r\n'


def test_timeout(bridge, simulator, cclient: AtClient):
    timeout = 1
    start_time = time.time()
    with pytest.raises(AtTimeout):
        cclient.send_command('AT!TIMEOUT?', timeout=timeout)
    assert int(time.time() - start_time) == timeout
    at_response = cclient.send_command('AT', timeout=3)
    assert at_response.ok


def test_bad_byte(bridge, simulator, cclient: AtClient):
    with pytest.raises(AtDecodeError):
        cclient.send_command('AT!BAD_BYTE?', timeout=2)


def test_response_plus_urc(bridge, simulator, cclient: AtClient):
    """What happens when one or more URCs immediately follow a response."""
    cmd_res = cclient.send_command('AT!MUDDLE?', timeout=5)
    assert cmd_res.ok is True
    urc_found = False
    while not urc_found:
        time.sleep(0.1)
        urc_found = cclient.check_urc()
    assert urc_found is True
