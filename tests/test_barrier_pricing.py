import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poly_arb_cli.services.barrier_pricing import no_touch_prob, one_touch_prob


def test_one_touch_monotonic_barrier_distance() -> None:
    close = one_touch_prob(spot=100, barrier=140, years=0.2, vol=0.2)
    far = one_touch_prob(spot=100, barrier=200, years=0.2, vol=0.2)
    assert close is not None and far is not None
    assert 0 <= close <= 1
    assert 0 <= far <= 1
    assert close > far  # barrier越近触及概率越大


def test_down_touch_probability_increases_when_barrier_rises() -> None:
    nearer = one_touch_prob(spot=100, barrier=90, years=0.2, vol=0.2, direction="down")
    farther = one_touch_prob(spot=100, barrier=70, years=0.2, vol=0.2, direction="down")
    assert nearer is not None and farther is not None
    assert nearer > farther


def test_no_touch_complements_touch() -> None:
    touch = one_touch_prob(spot=100, barrier=120, years=0.2, vol=0.4)
    nt = no_touch_prob(spot=100, barrier=120, years=0.2, vol=0.4)
    assert touch is not None and nt is not None
    assert math.isclose((touch + nt), 1.0, rel_tol=1e-3, abs_tol=1e-3)
