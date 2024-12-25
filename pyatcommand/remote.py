"""A remote serial server for connecting to a modem over a network.

Runs on a remote computer e.g. Raspberry Pi physically connected to a
modem that interfaces using AT commands.
"""

import logging
import os
import serial
import socket
import threading


SERIAL_PORT = os.getenv('SERIAL_PORT', '/dev/ttyUSB0')
BAUDRATE = int(os.getenv('BAUDRATE', '9600'))
HOST = os.getenv('SERIAL_HOST', '0.0.0.0')
PORT = int(os.getenv('SERIAL_TCP_PORT', '12345'))

_log = logging.getLogger(__name__)


class RemoteSerial:
    """A class for serving a remote serial connection over TCP."""

    def __init__(self,
                 port: str = SERIAL_PORT,
                 baudrate: int = BAUDRATE,
                 read_timeout: float = 1):
        self.ser = serial.Serial(port, baudrate, timeout=read_timeout)
    
    def start(self):
        """Start the serial server."""
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.bind((HOST, PORT))
        server_socket.listen(5)
        _log.info('Listening for serial data on %s:%d', HOST, PORT)
        while True:
            client_socket, addr = server_socket.accept()
            _log.info('Connection from %s', addr)
            client_thread = threading.Thread(target=self.handle_client,
                                            args=(client_socket,),
                                            daemon=None)
            client_thread.start()

    def handle_client(self, client_socket: socket.socket):
        """Allow a client to connect remotely."""
        try:
            while True:
                if self.ser.in_waiting > 0:
                    data = self.ser.read(self.ser.in_waiting)
                    client_socket.sendall(data)
                client_data = client_socket.recv(1024)
                if client_data:
                    self.ser.write(client_data)
                else:
                    break
        finally:
            client_socket.close()


if __name__ == '__main__':
    server = RemoteSerial()
    server.start()
