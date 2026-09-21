import yaml
from syncall.tw_asana_utils import convert_asana_to_tw, convert_tw_to_asana

from .generic_test_case import GenericTestCase


class TestTwAsanaConversions(GenericTestCase):
    """Test item conversions - TW <-> Asana."""

    def get_keys_to_match(self):
        return set(self.tw_item.keys()).intersection(
            ("description", "due", "modified", "status"),
        )

    def load_sample_items(self):
        with (GenericTestCase.DATA_FILES_PATH / "sample_items.yaml").open() as fname:
            conts = yaml.load(fname, Loader=yaml.Loader)  # noqa: S506

        self.asana_task = conts["asana_task"]
        self.tw_item_expected = conts["tw_item_expected"]

        self.tw_item = conts["tw_item"]
        self.asana_task_expected = conts["asana_task_expected"]

        self.tw_item_w_due = conts["tw_item_w_due"]

    def test_tw_asana_basic_convert(self):
        """Basic TW -> Asana conversion."""
        self.load_sample_items()
        asana_task_out = convert_tw_to_asana(self.tw_item)
        for key, value in self.asana_task_expected.items():
            assert asana_task_out[key] == value, key

        assert asana_task_out.html_notes == "<body></body>"
        assert asana_task_out.comments == tuple(str(a) for a in self.tw_item["annotations"])

    def test_asana_tw_basic_convert(self):
        """Basic Asana -> TW conversion."""
        self.load_sample_items()
        tw_item_out = convert_asana_to_tw(self.asana_task)
        for key in self.get_keys_to_match():
            assert tw_item_out[key] == self.tw_item_expected[key]

    def test_tw_asana_n_back(self):
        """TW -> Asana -> TW conversion"""
        self.load_sample_items()
        tw_item_out = convert_asana_to_tw(convert_tw_to_asana(self.tw_item))

        for key in self.get_keys_to_match():
            if key in self.tw_item:
                assert key in tw_item_out
                assert self.tw_item[key] == tw_item_out[key]
            if key in tw_item_out:
                assert key in self.tw_item
                assert tw_item_out[key] == self.tw_item[key]

    def test_asana_tw_n_back_basic(self):
        """Test Asana -> TW -> Asana conversion."""
        self.load_sample_items()
        asana_task_out = convert_tw_to_asana(convert_asana_to_tw(self.asana_task))

        for key in [
            "completed",
            "completed_at",
            "created_at",
            "due_at",
            "modified_at",
            "name",
        ]:
            if key in self.asana_task:
                assert key in asana_task_out
                assert self.asana_task[key] == asana_task_out[key]
            if key in asana_task_out:
                assert key in self.asana_task
                assert asana_task_out[key] == self.asana_task[key]

    def test_tw_asana_sets_both_due_dates(self):
        """Test that due dates are set in both TW and Asana."""
        self.load_sample_items()

        assert "due" in self.tw_item_w_due
        assert self.tw_item_w_due is not None

        asana_task = convert_tw_to_asana(self.tw_item_w_due)

        assert "due_at" in asana_task
        assert asana_task["due_at"] == self.tw_item_w_due["due"]
        assert "due_on" in asana_task
        assert asana_task["due_on"] == asana_task["due_at"].date()

    def test_client_prefix_is_split_and_reconstructed(self):
        self.load_sample_items()
        asana_task = dict(self.asana_task)
        asana_task["name"] = "[Color Wow] Review category pages"

        tw_item = convert_asana_to_tw(asana_task)

        assert tw_item["client"] == "Color Wow"
        assert tw_item["description"] == "Review category pages"

        round_trip = convert_tw_to_asana(tw_item)
        assert round_trip.name == "[Color Wow] Review category pages"

    def test_name_without_client_prefix_is_unchanged(self):
        self.load_sample_items()
        tw_item = convert_asana_to_tw(self.asana_task)

        assert "client" not in tw_item
        assert tw_item["description"] == self.asana_task["name"]

    def test_notes_convert_between_markdown_and_asana_html(self):
        self.load_sample_items()
        asana_task = dict(self.asana_task)
        asana_task["html_notes"] = (
            "<body><h2>Analysis</h2>Check <strong>revenue</strong>."
            "<ul><li>UK</li><li>US</li></ul></body>"
        )

        tw_item = convert_asana_to_tw(asana_task)

        assert "## Analysis" in tw_item["notes"]
        assert "**revenue**" in tw_item["notes"]
        assert "- UK" in tw_item["notes"]

        round_trip = convert_tw_to_asana(tw_item)
        assert "<h2>Analysis</h2>" in round_trip.html_notes
        assert "<strong>revenue</strong>" in round_trip.html_notes

    def test_comments_map_to_taskwarrior_annotations(self):
        self.load_sample_items()
        asana_task = dict(self.asana_task)
        asana_task["comments"] = ["First comment", "Second comment"]

        tw_item = convert_asana_to_tw(asana_task)

        assert tw_item["annotations"] == ["First comment", "Second comment"]

        round_trip = convert_tw_to_asana(tw_item)
        assert round_trip.comments == ("First comment", "Second comment")

    def test_blank_asana_description_is_skipped(self):
        self.load_sample_items()
        asana_task = dict(self.asana_task)
        asana_task["gid"] = "blank-1"
        asana_task["name"] = "   "

        assert convert_asana_to_tw(asana_task) is None

    def test_client_only_asana_name_is_skipped(self):
        self.load_sample_items()
        asana_task = dict(self.asana_task)
        asana_task["gid"] = "blank-2"
        asana_task["name"] = "[Color Wow]"

        assert convert_asana_to_tw(asana_task) is None
