import pytest

from data.stress_load import LineStream, row_range, split_point


def test_line_stream_returns_every_line_in_chunks():
    lines = [f"row-{i}\n" for i in range(1000)]
    stream = LineStream(iter(lines))
    out = ""
    while chunk := stream.read(100):
        assert len(chunk) <= 100
        out += chunk
    assert out == "".join(lines)


def test_line_stream_read_all():
    assert LineStream(iter(["a\n", "b\n"])).read() == "a\nb\n"


def test_initial_and_delta_cover_every_row_exactly_once():
    rows = 1_001
    initial = row_range("initial", rows, 0.9)
    delta = row_range("delta", rows, 0.9)
    assert initial == (0, 900) and delta == (900, rows)
    assert row_range("all", rows, 0.9) == (0, rows)


@pytest.mark.parametrize("fraction", [0, 1, -0.1, 1.5])
def test_split_point_rejects_bad_fraction(fraction):
    with pytest.raises(ValueError):
        split_point(100, fraction)
