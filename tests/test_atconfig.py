import pytest

from pyatcommand.common import AtConfig


def test_invalid_settings(caplog):
    config = AtConfig()
    with pytest.raises(ValueError):
        config.cr = '\r\n'
    with pytest.raises(ValueError):
        config.lf = '\r\n'
    with pytest.raises(ValueError):
        config.echo = 1
    with pytest.raises(ValueError):
        config.verbose = 0
    with pytest.raises(ValueError):
        config.bs = '\t'
    with pytest.raises(ValueError):
        config.crc_sep = '\r'
    config.terminator = '\r\n'
    assert any(
        record.levelname == 'WARNING' and 'multi-character' in record.message 
        for record in caplog.records
    )


def test_repr_str():
    config = AtConfig()
    r = repr(config)
    assert isinstance(r, str) and '<cr>' not in r
    s = str(config)
    assert isinstance(s, str) and '<cr>' in s
