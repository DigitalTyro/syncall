from types import SimpleNamespace

from syncall.progress import RateColumn, RemainingColumn


def _task(**overrides: object) -> SimpleNamespace:
    task = SimpleNamespace(
        speed=None,
        finished_speed=None,
        finished=False,
        finished_time=None,
        elapsed=None,
        completed=0,
        total=12,
    )
    for name, value in overrides.items():
        setattr(task, name, value)
    return task


def test_rate_uses_elapsed_time_when_rich_has_no_speed_sample() -> None:
    text = RateColumn("ops").render(_task(elapsed=2.0, completed=4))

    assert str(text) == "2.0 ops/s"


def test_rate_stays_blank_before_any_step_finishes() -> None:
    text = RateColumn("ops").render(_task(elapsed=2.0, completed=0))

    assert str(text) == "-- ops/s"


def test_remaining_time_uses_the_same_elapsed_rate() -> None:
    text = RemainingColumn().render(_task(elapsed=2.0, completed=4, total=12))

    assert str(text) == "0:00:04"
