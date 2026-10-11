"""pytest fixtures for test_sstv.py.

test_sstv.py is a script-style file (./bin/python3 test_sstv.py runs
main(), which calls test_mode_round_trip(mode, sample_rate) etc. with
explicit arguments). Under pytest those same functions are collected
as tests, so supply the arguments main() would. Nothing else in the
suite uses these fixture names.
"""

import pytest

import sstv


@pytest.fixture
def sample_rate():
    return 11025  # same value main() uses: fast, with enough pixel-timing resolution


@pytest.fixture(params=[sstv.ROBOT36, sstv.MARTIN_M1, sstv.SCOTTIE_S1], ids=lambda m: m.name)
def mode(request):
    return request.param
