import os

from syncall.taskwarrior.taskwarrior_side import (
    ensure_task_rc_include_dir,
    merge_config_overrides,
    tw_client_key,
    tw_notes_key,
)
from taskw_ng.taskrc import TaskRc


def test_asana_udas_are_enabled_by_default() -> None:
    merged = merge_config_overrides({})

    assert merged["uda"][tw_client_key]["type"] == "string"
    assert merged["uda"][tw_notes_key]["type"] == "string"


def test_custom_uda_overrides_do_not_remove_asana_udas() -> None:
    merged = merge_config_overrides(
        {"uda": {"estimate": {"type": "duration", "label": "Estimate"}}},
    )

    assert tw_client_key in merged["uda"]
    assert tw_notes_key in merged["uda"]
    assert merged["uda"]["estimate"]["type"] == "duration"


def test_task_rcdir_is_left_unchanged_when_already_set(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TASK_RCDIR", str(tmp_path))

    ensure_task_rc_include_dir()

    assert os.environ["TASK_RCDIR"] == str(tmp_path)


def test_discovered_theme_directory_satisfies_default_theme_include(
    tmp_path, monkeypatch
) -> None:
    include_dir = tmp_path / "share" / "doc" / "task" / "rc"
    include_dir.mkdir(parents=True)
    (include_dir / "default.theme").write_text("rule.precedence.color=off\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "task").write_text("")
    monkeypatch.delenv("TASK_RCDIR", raising=False)
    monkeypatch.setattr(
        "syncall.taskwarrior.taskwarrior_side.shutil.which",
        lambda command: str(bindir / "task") if command == "task" else None,
    )
    taskrc = tmp_path / ".taskrc"
    taskrc.write_text("include default.theme\n")

    ensure_task_rc_include_dir()
    loaded = TaskRc(str(taskrc))

    assert loaded["rule"]["precedence"]["color"] == "off"
