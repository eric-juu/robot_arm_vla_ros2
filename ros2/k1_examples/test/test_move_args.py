import pytest

from k1_examples.move import parse_positions


def test_six_numbers():
    assert parse_positions(["0.3", "0", "0", "0", "0", "-0.1"]) == [0.3, 0, 0, 0, 0, -0.1]


def test_named_joints():
    assert parse_positions(["joint1=0.3", "shoulder_lift=-0.2"]) == {"joint1": 0.3, "shoulder_lift": -0.2}


@pytest.mark.parametrize("items", [["0.3", "0"], ["joint1=0.3", "0.2"]])
def test_bad_input(items):
    with pytest.raises(ValueError):
        parse_positions(items)
