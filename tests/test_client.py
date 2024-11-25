import logging
import pytest

from pyatcommand import AtClient, AtErrorCode
from .simulator.socat import SerialBridge, ModemSimulator, DTE, COMMAND_FILE

logger = logging.getLogger(__name__)


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


def test_connect(bridge: SerialBridge, simulator: ModemSimulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5)
    assert client.is_connected()


def test_response(bridge: SerialBridge, simulator: ModemSimulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5)
    assert client.send_at_command('AT+GMI') == AtErrorCode.OK
    response = client.get_response()
    assert isinstance(response, str) and len(response) > 0


def test_response_trim(bridge: SerialBridge, simulator: ModemSimulator, client: AtClient):
    client.connect(port=DTE, retry_timeout=5)
    assert client.send_at_command('AT+CGDCONT?') == AtErrorCode.OK
    response = client.get_response('+CGDCONT:')
    assert isinstance(response, str) and len(response) > 0 and '+CGDCONT' not in response
