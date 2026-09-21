from syncall.taskwarrior.taskwarrior_side import (
    merge_config_overrides,
    tw_client_key,
    tw_notes_key,
)


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
