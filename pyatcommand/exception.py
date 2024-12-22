"""Base classes for AT command exceptions.
"""

class AtException(Exception):
    """Base class for AT command exceptions."""


class AtTimeout(AtException):
    """Indicates a timeout waiting for response."""


class AtCrcConfigError(AtException):
    """Indicates a CRC response was received when none expected or vice versa."""


class AtCrcError(AtException):
    """Indicates a detected CRC mismatch on a response."""


class AtDecodeError(AtException):
    """Indicates non-ASCII or non-printable byte received."""


class AtUnsolicited(AtException):
    """Indicates unsolicited data was received from the modem."""
