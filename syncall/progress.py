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
    TimeRemainingColumn,
)
from rich.text import Text


class RateColumn(ProgressColumn):
    """Display processing throughput using a caller-provided unit."""

    def __init__(self, unit: str) -> None:
        super().__init__()
        self._unit = unit

    def render(self, task: Task) -> Text:
        speed = task.speed
        if speed is None:
            return Text(f"-- {self._unit}/s")
        return Text(f"{speed:.1f} {self._unit}/s")


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
        TimeRemainingColumn(),
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
