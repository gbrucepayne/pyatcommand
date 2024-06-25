# V.25 AT Command Parser for Python

This was developed due to seeming lack of a generalized AT command processing
library in the PyPI ecosystem. Several other implementations exist for more
specific purposes but miss certain functions such as non-verbose (`V0`) mode,
echo on/off, or unsolicited result codes.

## Client

The client functionality is used to talk to a modem (or anything similar that
supports AT commands).

Allows for processing of command/response or receipt of unsolicited result code
(URC) emitted by the modem. Also includes an optional CRC validation supported
by some modems.

### Command/Response

This is the main mode of intended use. The logic flow is as follows:

1. AT commmand, with optional timeout, is submitted by a function call
`send_at_command()` which:
    * If a prior command is pending (TBC thread-safe) waits for a `ready`
    Event to be set by the completion of the prior command;
    * Clears the last error code;
    * Clears the receive buffer;
    * (Optional) calculates and applies CRC to the command;
    * Applies the command line termination character (default `\r`);
    * Sends the command on serial and waits for all data to be sent;
    * Sets the pending command state;
    * Calls an internal response parsing function and returns an `AtErrorCode`
    code, with 0 (`OK`) indicating success;
    * If no timeout is specified, the default is 1 second
    (`AT_TIMEOUT`).

2. Response parsing:
    * Transitions through states `ECHO`, `RESPONSE`, (*optional*) `CRC`
    to either `OK` or `ERROR`;
    * If timeout is exceeded, parsing stops and indicates
    `AtErrorCode.ERR_TIMEOUT`;
    * (Optional) validation of checksum, failure indicates
    `AtErrorCode.ERR_CMD_CRC`;
    * Other modem error codes received will be indicated transparently;
    * Successful parsing will place the response into a buffer for retrieval;
    * Sets the last error code or `OK` (0) if successful;
    * Clears the pending command state, and sets the `ready` Event.

3. Retrieval of successful response is done using `get_response()`
with an optional `prefix` to remove.
All other leading/trailing whitespace is removed, and multi-line responses are
separated by a single line feed (`\n`). Retrieval clears the *get* buffer.

4. A function `last_error_code()` is intended to be defined for modems
that support this concept (e.g. query `S80?` on Orbcomm satellite modem).

### Unsolicited Result Codes (URC)

Some modems emit unsolicited codes. In these cases it is recommended that the
application checks/retrieves any URC(s) prior to submitting any AT command.

`check_urc()` simply checks if any serial data is waiting when no AT command is
pending, and if present parses until both command line termination and response
formatting character have been received or timeout (default 1 second
`AT_URC_TIMEOUT`).
URC data is placed in the *get* buffer and retrieved in the same way as a
commmand response.

### CRC support

Currently a CCITT-16-CRC option is supported for commands and responses. The
enable/disable command may be configured using `+CRC=<1|0>`.
(`%CRC=<1|0>` also works)

## Server (Work in Progress)

The server concept is to act as a modem/proxy replying to a microcontroller.

You register custom commands using `add_command()` with a data structure that
includes the command `name` and optional callback functions for `read`, `run`,
`test` and `write` operations.

`Verbose` and `Echo` features are supported using the standard `V` and `E`
commands defined in the V.25 spec.

`CRC` is an optional extended command to support 16-bit checksum validation of
requests and responses that can be useful in noisy environments.

### Feature considerations

* Repeating a command line using `A/` or `a/` is not supported;
* No special consideration is given for numeric or string constants, those are
left to custom handling functions;
* Concatenation of basic commands deviates from the standard and expects a
semicolon separator;

### Acknowledgements

The server idea is based somewhat on the
[ATCommands](https://github.com/yourapiexpert/ATCommands)
library which had some shortcomings for my cases including GPL, and
[cAT](https://github.com/marcinbor85/cAT) but reframed for C++.
Many thanks to those developers for some great ideas!