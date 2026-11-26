import pytest
from dsy_servicekit.config import (
    ConfigError,
    exiting_on_config_error,
    host_port,
    positive,
    require,
)


def test_require_returns_fields():
    assert require({"A": "1", "B": "2"}, {"a": "A", "b": "B"}) == {"a": "1", "b": "2"}


def test_require_names_every_absent_or_empty_variable():
    with pytest.raises(ConfigError, match="^missing environment: A, C$"):
        require({"A": "", "B": "2"}, {"a": "A", "b": "B", "c": "C"})


@pytest.mark.parametrize("raw, want", [("2", 2.0), ("0.5", 0.5), (None, 7.0), ("", 7.0)])
def test_positive(raw, want):
    environ = {} if raw is None else {"N": raw}
    assert positive(environ, "N", default=7) == want


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "nan", "inf"])
def test_positive_refuses(raw):
    with pytest.raises(ConfigError, match="N is .* not a positive number"):
        positive({"N": raw}, "N", default=7)


def test_positive_without_default_requires_the_variable():
    with pytest.raises(ConfigError, match="missing environment: N"):
        positive({}, "N")


def test_host_port():
    assert host_port("example.org:5432", "ADDR") == ("example.org", 5432)
    assert host_port("[::1]:80", "ADDR") == ("[::1]", 80)


@pytest.mark.parametrize(
    "value", ["", "host", "host:", ":80", "host:x", "host:0", "host:65536", "host:-1"]
)
def test_host_port_refuses(value):
    with pytest.raises(ConfigError, match="ADDR is .* not host:port"):
        host_port(value, "ADDR")


def test_exiting_on_config_error(capsys):
    with pytest.raises(SystemExit) as exit_:
        with exiting_on_config_error("svc"):
            raise ConfigError("bad")
    assert exit_.value.code == 2
    assert capsys.readouterr().err == "svc: bad\n"


def test_other_errors_pass_through():
    with pytest.raises(KeyError):
        with exiting_on_config_error("svc"):
            raise KeyError("x")
