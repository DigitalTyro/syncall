from math import ceil

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text


def _steps_per_second(task: Task) -> float | None:
    """Return a rate even when Rich has only one progress sample.

    Rich's own speed stays empty until two samples have different timestamps, so a
    stage that finishes a single step otherwise displays "--" for the whole run.
    """
    speed = task.finished_speed if task.finished else task.speed
    if speed:
        return speed
    elapsed = task.finished_time if task.finished else task.elapsed
    if elapsed and task.completed:
        return task.completed / elapsed
    return None


class RateColumn(ProgressColumn):
    """Display processing throughput using a caller-provided unit."""

    def __init__(self, unit: str) -> None:
        super().__init__()
        self._unit = unit

    def render(self, task: Task) -> Text:
        speed = _steps_per_second(task)
        if not speed:
            return Text(f"-- {self._unit}/s")
        return Text(f"{speed:.1f} {self._unit}/s")


class RemainingColumn(ProgressColumn):
    """Display time remaining from the same rate shown beside the bar."""

    def render(self, task: Task) -> Text:
        if task.finished:
            return Text("0:00:00", style="progress.remaining")
        speed = _steps_per_second(task)
        if not speed or task.total is None:
            return Text("-:--:--", style="progress.remaining")
        remaining = max(0, ceil((task.total - task.completed) / speed))
        minutes, seconds = divmod(remaining, 60)
        hours, minutes = divmod(minutes, 60)
        return Text(f"{hours:d}:{minutes:02d}:{seconds:02d}", style="progress.remaining")


class CountColumn(ProgressColumn):
    """Display a comma-formatted completed count for indeterminate work."""

    def __init__(self, unit: str) -> None:
        super().__init__()
        self._unit = unit

    def render(self, task: Task) -> Text:
        return Text(f"{int(task.completed):,} {self._unit}")


def make_progress(*, console: Console, unit: str) -> Progress:
    """Create a standard determinate syncall progress display."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}[/bold]"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        RateColumn(unit),
        TimeElapsedColumn(),
        RemainingColumn(),
        console=console,
    )


def make_indeterminate_progress(*, console: Console, unit: str) -> Progress:
    """Create a standard progress display for work whose total is not yet known."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}[/bold]"),
        CountColumn(unit),
        TimeElapsedColumn(),
        console=console,
    )
