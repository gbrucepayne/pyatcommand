"""Unit test cases for AtClient."""
import logging
import os
import random
import threading
import time

import pytest

from pyatcommand import AtErrorCode, AtTimeout, AtClient
from pyatcommand.common import list_available_serial_ports

from simulator import ModemSimulator, SerialBridge

logger = logging.getLogger(__name__)

REAL_UART = os.getenv('REAL_UART')


def test_connect(bridge: SerialBridge, simulator: ModemSimulator):
    client = AtClient()
    client.connect(port=bridge.dte, retry_timeout=2)
    assert client.is_connected()
    client.disconnect()


def test_connect_no_port():
    client = AtClient()
    with pytest.raises(ConnectionError) as excinfo:
        client.connect()
    assert 'invalid or missing' in str(excinfo.value).lower()


def test_connect_invalid_port():
    client = AtClient()
    with pytest.raises(ConnectionError) as excinfo:
        client.connect(port='COM99')
    assert 'unable to open' in str(excinfo.value).lower()


def test_connect_no_response(bridge: SerialBridge):
    client = AtClient()
    with pytest.raises(ConnectionError) as excinfo:
        client.connect(port=bridge.dte, retry_timeout=2)
    assert 'timed out' in str(excinfo.value).lower()


@pytest.mark.skipif(not any(port == REAL_UART for port in list_available_serial_ports()),
                    reason = f'{REAL_UART} not available')
def test_autobaud(log_verbose):
    """Testing autobaud requires use of a physical serial port.
    
    Simulated DTE does not care about baud rate of pyserial.
    """
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


def test_echo_autodetect(simulator: ModemSimulator, cclient: AtClient):
    assert cclient.echo is True
    simulator.echo = False
    cclient.send_command('AT')
    assert cclient.echo is False
    simulator.echo = True
    cclient.send_command('AT')
    assert cclient.echo is True


def test_verbose_autodetect(simulator: ModemSimulator, cclient: AtClient):
    assert cclient.verbose is True
    simulator.verbose = False
    cclient.send_command('AT')
    assert cclient.verbose is False
    simulator.verbose = True
    cclient.send_command('AT')
    assert cclient.verbose is True


def test_send_command(simulator, cclient: AtClient):
    at_response = cclient.send_command('AT+GMI')
    assert at_response.ok
    assert isinstance(at_response.info, str)


def test_non_verbose(simulator: ModemSimulator, cclient: AtClient):
    """Test responses with V0"""
    v0_response = cclient.send_command('ATV0')
    assert v0_response.ok is True
    assert cclient.verbose is False
    at_response = cclient.send_command('AT+GMI')
    assert at_response.ok is True
    assert isinstance(at_response.info, str) and len(at_response.info) > 0


def test_send_command_crc(simulator, cclient: AtClient, caplog):
    cclient.crc_enable = 'AT%CRC=1'
    assert cclient.crc_disable == 'AT%CRC=0'
    assert cclient.crc is False
    at_response = cclient.send_command('AT%CRC=1')
    assert at_response.ok is True
    assert at_response.crc_ok is True
    assert cclient.crc is True
    at_response = cclient.send_command('AT%CRC=0')
    assert at_response.ok is False
    assert at_response.crc_ok is True
    assert cclient.crc is True
    res = cclient.send_command('AT+BADCRC?', timeout=90)
    assert res.ok is False
    assert res.crc_ok is False
    assert any(
        record.levelname == 'WARNING' and 'invalid crc' in record.message.lower()
        for record in caplog.records
    )
    at_response = cclient.send_command('AT%CRC=0*BBEB')
    assert at_response.ok
    assert at_response.crc_ok is None
    assert cclient.crc is False


def test_command_prefix(simulator, cclient: AtClient):
    command = 'AT+CGDCONT?'
    prefix = '+CGDCONT:'
    at_response = cclient.send_command(command)
    assert len(at_response.info) > 0 and prefix in at_response.info
    at_response = cclient.send_command(command, prefix=prefix)
    assert at_response.ok
    assert len(at_response.info) > 0 and prefix not in at_response.info


def test_get_urc(simulator: ModemSimulator, cclient:AtClient):
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


def test_multiline(simulator: ModemSimulator, cclient: AtClient):
    """Multiline responses"""
    response = cclient.send_command('ATI')
    assert response.ok and len(response.info.split('\n')) > 1
    single_crlf_spacer_response = response.info.split('\n')
    response_2 = cclient.send_command('ATI1')
    assert response_2.info.split('\n') == single_crlf_spacer_response


def test_multi_urc(simulator: ModemSimulator, cclient: AtClient):
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


def test_multi_urc_v0(simulator: ModemSimulator, cclient: AtClient):
    simulator.verbose = False
    urcs = [
        '%NOTIFY:"RRCSTATE",2',
        '%NOTIFY:"RRCSTATE",0',
        '%NOTIFY:"RRCSTATE",2',
        '+CEREG: 0,,,,,,,"00111000"',
    ]
    # test with default URC header '\r\n'
    simulator.multi_urc(urcs)
    received_count = 0
    while received_count < len(urcs):
        if cclient.get_urc():
            received_count += 1
    assert received_count == len(urcs)
    # test with no URC header
    simulator.multi_urc(urcs, '')
    received_count = 0
    while received_count < len(urcs):
        if cclient.get_urc():
            received_count += 1
    assert received_count == len(urcs)


def test_urc_send_race(simulator: ModemSimulator, cclient: AtClient):
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


def test_cme_error(simulator: ModemSimulator, cclient: AtClient):
    at_response = cclient.send_command('AT+CMEE=4')
    assert at_response.ok is False
    assert at_response.info == 'invalid configuration'


def test_timeout(simulator, cclient: AtClient):
    timeout_cmd = 'AT!TIMEOUT?'
    delay = 3
    timeout = 1
    start_time = time.time()
    with pytest.raises(AtTimeout):
        cclient.send_command(timeout_cmd, timeout=timeout)
    assert int(time.time() - start_time) == timeout
    time.sleep(delay)
    at_response = cclient.send_command(timeout_cmd, timeout=delay+0.5)
    assert at_response.ok


def test_bad_byte(simulator, cclient: AtClient, caplog):
    for p in ['B', 'M', 'E']:
        res = cclient.send_command(f'AT!BAD_BYTE_{p}?', timeout=2)
        assert res.ok is True
        assert any('invalid char' in message.lower() for message in caplog.messages)


def test_response_plus_urc(simulator, cclient: AtClient):
    """What happens when one or more URCs immediately follow a response."""
    cmd_res = cclient.send_command('AT!MUDDLE?', timeout=5)
    assert cmd_res.ok is True
    urc_found = False
    while not urc_found:
        time.sleep(0.1)
        urc_found = cclient.check_urc()
    assert urc_found is True


def test_noncompliant_response(simulator, cclient: AtClient, log_verbose):
    """Check noncompliant response handling."""
    # Seen on Murata/Sony response to AT%GETACFG="ntn.conf.gnss_in_use"
    at_response = cclient.send_command('AT!NONCOMPLY?')
    assert at_response.ok is True
    assert len(at_response.info) > 0
    assert at_response.info == 'Missing trailer'
    at_response = cclient.send_command('ATV0', timeout=90)
    assert cclient.verbose is False
    at_response = cclient.send_command('AT!V0NONCOMPLY?', timeout=90)
    assert at_response.ok is True
    assert at_response.info == 'Missing trailer'


def test_urc_echo_race(simulator, cclient: AtClient, log_verbose):
    """Case when URC arrives as AT command is being sent, before echo received."""
    # disable echo on simulator to create test response
    simulator.echo = False
    at_response = cclient.send_command('AT!ECHORACE?')
    assert at_response.ok is True


def test_urc_response_race(simulator, cclient: AtClient, log_verbose):
    """Case when URC arrives after command but before response."""
    # Seen on Murata/Sony with +CEREG output between command and response
    at_response = cclient.send_command('AT!RESURCRACE?', prefix='!RESURCRACE:', timeout=3)
    assert at_response.ok is True
    urc = cclient.get_urc()
    assert urc is not None


def test_intermediate_callback(simulator: ModemSimulator, cclient: AtClient, log_verbose, caplog):
    """Case when an intermediate result triggers a callback."""
    
    def icb():
        logger.info('Received intermediate callback')
        simulator.intermediate_pause = False
    
    mid_prompt = '>'
    res = cclient.send_command('AT!INTERMEDIATE=X',
                               timeout=90,
                               mid_prompt=mid_prompt,
                               mid_cb=icb)
    assert res.ok and isinstance(res.info, str) and mid_prompt in res.info
    assert any(
        record.levelname == 'INFO' and 'intermediate' in record.message
        for record in caplog.records
    )


def test_send_bytes_data_mode_intermediate(simulator: ModemSimulator, cclient: AtClient, log_verbose, caplog):
    """Send data where an intermediate result triggers data mode in between final result.
    
    Example: SIMCOM SIM7070G modem
    """
    test_data = b'Test send intermediate data mode'
    test_data_len = len(test_data)
    
    def send_data_mode_intermediate(data):
        logger.info('Processing data mode send callback')
        cclient.send_bytes_data_mode(data, auto=True)
    
    res = cclient.send_command(f'AT+ISENDDATAMODE=1,{test_data_len}',
                               timeout=90,
                               mid_prompt='\r\n>',
                               mid_cb=send_data_mode_intermediate,
                               mid_cb_args=(test_data,),
                               )
    assert res.ok
    assert simulator.data_mode_data == test_data
    simulator.data_mode_data.clear()
    simulator.data_mode = False


def test_recv_bytes_data_mode_intermediate(simulator: ModemSimulator, cclient: AtClient, log_verbose, caplog):
    """Receive data where an intermediate result triggers data mode in between final result.
    
    Example: Skywave IDP modem (ignoring xmodem implementation)
    """
    received_bytes: 'bytes|None' = None
    expected = b'Test recv intermediate data mode'
    
    def recv_data_mode_intermediate():
        nonlocal received_bytes
        logger.info('Processing data mode receive callback')
        cclient.data_mode = True
        time.sleep(0.1)   # yield to simulator to send data
        received_bytes = cclient.recv_bytes_data_mode(timeout=2)
        logger.info('Received %d bytes in data mode', len(received_bytes))
        cclient.data_mode = False
        time.sleep(0.1)   # yield to simulator to send closure
    
    res = cclient.send_command('AT+IRECVDATAMODE=1,1200',
                               timeout=90,
                               mid_prompt='\r\n+IRECVDATAMODE:',
                               mid_cb=recv_data_mode_intermediate)
    assert res.ok
    assert isinstance(res.info, str) and len(res.info) > 0
    assert isinstance(received_bytes, bytes) and len(received_bytes) > 0
    assert received_bytes == expected


def test_send_bytes_data_mode_sequential(simulator: ModemSimulator, cclient: AtClient, log_verbose, caplog):
    """Case where the command triggers data mode after the final result.
    
    Example: Simcom SIM7070 switch to transparent mode
    """
    test_data = b'Test send switched data mode'
    # Simcom style, use context 0 to distinguish send from receive simulation
    data_mode_exit_sequence = b'+++'
    res = cclient.send_command('AT+CASWITCH=0,1', timeout=90)
    time.sleep(0.25)   # allow simulator and URC to process
    assert res.ok is True
    deadline = time.time() + 2
    while cclient.get_urc() != 'CONNECT':
        if time.time() > deadline:
            raise IOError('Timed out waiting for data mode prompt')
        time.sleep(0.1)
    cclient.data_mode = True
    logger.debug('Sending data')
    cclient.send_bytes_data_mode(test_data)
    time.sleep(1)   # delay for processing by simulator
    logger.debug('Sending exit sequence')
    cclient.send_bytes_data_mode(data_mode_exit_sequence)
    time.sleep(1)
    assert simulator.data_mode is False
    cclient.data_mode = False
    time.sleep(0.1)   # allow simulator to process
    assert simulator.data_mode_data == test_data
    simulator.data_mode_data.clear()
    assert cclient.get_urc() == 'OK'
    assert cclient.send_command('AT').ok


def test_recv_bytes_data_mode_sequential(simulator: ModemSimulator, cclient: AtClient, log_verbose, caplog):
    """Receive data where modem is switched in and out of data mode by commands.
    
    Example: Simcom SIM7070 transparent mode
    Example: Nordic nRF91xx modem (TBC case of binary vs ascii output)
    """
    received_bytes: 'bytes|None' = None
    expected = b'Test recv switched data mode'
    timeout = 80
    # Simcom style, use context 1 to distinguish receive from send simulation
    exit_data_mode = b'+++'
    res = cclient.send_command('AT+CASWITCH=1,1', timeout=90)
    time.sleep(0.25)   # allow simulator and URC to process
    if res.ok:
        deadline = time.time() + 2
        while cclient.get_urc() != 'CONNECT':
            if time.time() > deadline:
                raise IOError('Timed out waiting for data mode prompt')
            time.sleep(0.1)
        cclient.data_mode = True
        received_bytes = cclient.recv_bytes_data_mode(timeout=timeout)
        assert isinstance(received_bytes, bytes) and received_bytes == expected
        time.sleep(1)   # delay to distinguish from data
        cclient.send_bytes_data_mode(exit_data_mode)
        time.sleep(1)
        cclient.data_mode = False
        time.sleep(0.1)   # allow simulator to process
        assert cclient.get_urc() == 'OK'
        assert cclient.send_command('AT').ok
    else:
        assert False


def test_send_xmodem(simulator: ModemSimulator, xclient: AtClient):
    """Test case for data mode using XMODEM protocol."""
    logging.getLogger('xmodem').setLevel(logging.DEBUG)
    data_to_send = b'Test sending XMODEM data'
    resp = xclient.send_command(f'AT+XMODEMSEND={len(data_to_send)}',
                                timeout=10,
                                mid_prompt='C',
                                mid_cb=xclient.send_bytes_data_mode,
                                mid_cb_args=(data_to_send,),
                                mid_cb_kwargs={'dce': simulator})
    assert resp.ok is True
    assert simulator.data_mode_data == data_to_send


def test_recv_xmodem(simulator: ModemSimulator, xclient: AtClient, log_verbose):
    """Test case for receiving data using XMODEM protocol.
    
    DTE uses an intermediate result from the DCE that it has entered data mode.
    An alternate approach would be a final result then explicit call to
    `recv_bytes_data_mode`.
    """
    expected = b'Test receiving XMODEM data'
    
    def data_callback(data: bytes):
        logger.info('Received: %r', data.rstrip(b'\x1a'))
        assert data.rstrip(b'\x1a') == expected
    
    logging.getLogger('xmodem').setLevel(logging.DEBUG)
    resp = xclient.send_command(f'AT+XMODEMRECV={len(expected)}',
                                timeout=10,
                                mid_prompt='+XMODEMRECV:',
                                mid_cb=xclient.recv_bytes_data_mode,
                                mid_cb_kwargs={
                                    'data_callback': data_callback,
                                    'dce': simulator,
                                })
    assert resp.ok is True


def test_false_config_detect(simulator, cclient: AtClient, log_verbose):
    """Checks the case where response looks similar to V0 response."""
    resp = cclient.send_command('AT!V0LIKE?', prefix='!V0LIKE:')
    assert resp.ok is True
    assert resp.info == '0'
    assert cclient.echo is True
    assert cclient.verbose is True


# Legacy test cases -----------------------------------------------------------

@pytest.mark.legacy
def test_legacy_response(simulator: ModemSimulator, cclient: AtClient):
    assert cclient.send_at_command('AT+GMI', timeout=3) == AtErrorCode.OK
    response = cclient.get_response()
    assert response == 'Simulated Modems Inc'
    assert cclient.send_at_command('ATI') == AtErrorCode.OK
    response = cclient.get_response()
    assert response == 'First line\nSecond line'
    assert cclient.send_at_command('AT') == AtErrorCode.OK
    response = cclient.get_response()
    assert response == ''


@pytest.mark.legacy
def test_legacy_response_prefix(simulator: ModemSimulator, cclient: AtClient):
    assert cclient.send_at_command('AT+CGDCONT?', timeout=3) == AtErrorCode.OK
    response = cclient.get_response('+CGDCONT:')
    assert isinstance(response, str) and len(response) > 0 and '+CGDCONT' not in response


@pytest.mark.legacy
def test_legacy_check_urc(simulator: ModemSimulator, cclient: AtClient):
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

    
@pytest.mark.legacy
def test_legacy_then_urc(simulator: ModemSimulator, cclient: AtClient):
    urc = '+URC: Test'
    assert cclient.send_at_command('AT+GMI', timeout=3) == AtErrorCode.OK
    assert cclient.is_response_ready() is True
    response = cclient.get_response()
    assert cclient.is_response_ready() is False
    assert response == 'Simulated Modems Inc'
    assert cclient.ready
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
    assert cclient.is_response_ready() is False
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


@pytest.mark.legacy
def test_legacy_cme_error(simulator: ModemSimulator, cclient: AtClient, log_verbose):
    assert cclient.send_at_command('AT+CMEE=4') == AtErrorCode.CME_ERROR
    assert cclient.is_response_ready() is True
    res = cclient.get_response()
    assert res == 'invalid configuration'
    cclient.send_at_command('AT+CMEE=4')
    assert cclient.get_response(clean=False) == '\r\n+CME ERROR: invalid configuration\r\n'


@pytest.mark.legacy
def test_legacy_urc_response_race(simulator, cclient: AtClient, log_verbose):
    """Case when URC arrives after command but before response."""
    # Seen on Murata/Sony with +CEREG output between command and response
    assert cclient.send_at_command('AT!RESURCRACE?', timeout=3) == AtErrorCode.OK
    response = cclient.get_response('!RESURCRACE:')
    assert len(response) > 0
    assert cclient.check_urc() is True
