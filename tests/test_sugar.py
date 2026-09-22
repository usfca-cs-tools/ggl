"""The `node.<portname>` authoring sugar is opt-in. It installs a __getattribute__
override that runs on every attribute access, so it stays OFF by default (native
attribute speed on the simulation hot path); a hand-author turns it on explicitly.
"""

from ggl import arithmetic
from ggl.node import Connector, enable_sugar, disable_sugar


def test_sugar_is_off_by_default():
    # With the sugar off, node.<port> is plain attribute access: a port name that
    # collides with a class constant (Adder.a == 'a') just returns that constant.
    adder = arithmetic.Adder(bits=8)
    assert not isinstance(adder.a, Connector)
    assert adder.a == 'a'


def test_enable_sugar_then_disable_restores_default():
    adder = arithmetic.Adder(bits=8)
    try:
        enable_sugar()
        assert isinstance(adder.a, Connector)          # port sugar resolves
        assert isinstance(adder.output('sum'), Connector)  # normal methods still work
    finally:
        disable_sugar()  # never leak the global override into other tests
    assert not isinstance(adder.a, Connector)          # back to native access
