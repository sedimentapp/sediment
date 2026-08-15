import pytest

from sediment.spaces import SpaceDerivationError, SpaceResolver

MM_PROFILE = {
    "mattermost": {
        "channels": [
            {"id": "3tgwj6wrx3yu5rrm9mkkwth93h", "name": "infra"},
            {"id": "agohektg3pyiiqb4qqtuw1h4ry", "name": "DM Roman M"},
            "bareidchannelxxxxxxxxxxxxx",
        ]
    }
}


def spaces(profile: dict) -> SpaceResolver:
    return SpaceResolver.from_profile(profile)


class TestYoutrack:
    def test_issue(self):
        assert spaces({}).derive("youtrack", "youtrack/VN-242.md") == ("yt:VN", "VN")

    def test_article(self):
        assert spaces({}).derive("youtrack", "youtrack/INF-A-5.md") == ("yt:INF", "INF")

    def test_no_dash_fails(self):
        with pytest.raises(SpaceDerivationError):
            spaces({}).derive("youtrack", "youtrack/UNKNOWN.md")


class TestMattermost:
    def test_known_dir(self):
        space, name = spaces(MM_PROFILE).derive("mattermost", "mattermost/infra/rootid1.md")
        assert space == "mm:3tgwj6wrx3yu5rrm9mkkwth93h"
        assert name == "infra"

    def test_dm_dir(self):
        space, _ = spaces(MM_PROFILE).derive(
            "mattermost", "mattermost/DM Roman M/rootid2.2026-04-17T12-30.md"
        )
        assert space == "mm:agohektg3pyiiqb4qqtuw1h4ry"

    def test_bare_string_entry(self):
        space, name = spaces(MM_PROFILE).derive(
            "mattermost", "mattermost/bareidchannelxxxxxxxxxxxxx/r.md"
        )
        assert space == "mm:bareidchannelxxxxxxxxxxxxx"
        assert name == "bareidchannelxxxxxxxxxxxxx"

    def test_unknown_dir_fails(self):
        with pytest.raises(SpaceDerivationError, match="renamed or removed"):
            spaces(MM_PROFILE).derive("mattermost", "mattermost/old-name/rootid.md")

    def test_absent_map_is_config_error(self):
        with pytest.raises(RuntimeError, match="--config-dir"):
            spaces({}).derive("mattermost", "mattermost/infra/rootid.md")

    def test_flat_layout_fails(self):
        with pytest.raises(SpaceDerivationError, match="layout"):
            spaces(MM_PROFILE).derive("mattermost", "mattermost/rootid.md")

    def test_duplicate_dir_key_fails(self):
        profile = {
            "mattermost": {
                "channels": [
                    {"id": "aaa", "name": "same"},
                    {"id": "bbb", "name": "same"},
                ]
            }
        }
        with pytest.raises(RuntimeError, match="Ambiguous"):
            SpaceResolver.from_profile(profile)

    def test_duplicate_identical_entries_allowed(self):
        profile = {
            "mattermost": {
                "channels": [
                    {"id": "aaa", "name": "same"},
                    {"id": "aaa", "name": "same"},
                ]
            }
        }
        space, _ = SpaceResolver.from_profile(profile).derive("mattermost", "mattermost/same/r.md")
        assert space == "mm:aaa"


def test_source_without_importer_fails():
    """A source whose plugin is not installed here must not be indexed spaceless."""
    with pytest.raises(SpaceDerivationError, match="no importer installed"):
        spaces({}).derive("wiki", "wiki/page.md")


class TestMakeSpace:
    def test_unknown_kind_fails(self):
        from knowledge_schema import make_space

        with pytest.raises(ValueError, match="Unknown space kind"):
            make_space("slack", "general")

    def test_empty_key_fails(self):
        from knowledge_schema import make_space

        with pytest.raises(ValueError, match="Empty space key"):
            make_space("youtrack", "")
