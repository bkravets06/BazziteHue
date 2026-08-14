import json

import pytest

from bazzitehue.config import Config, Scene, is_address
from bazzitehue.errors import ConfigError

ADDRESS = "AA:BB:CC:DD:EE:FF"
OTHER = "11:22:33:44:55:66"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "config.json"


class TestAddress:
    @pytest.mark.parametrize("value", [ADDRESS, "aa:bb:cc:dd:ee:ff"])
    def test_accepts_macs(self, value):
        assert is_address(value)

    @pytest.mark.parametrize("value", ["desk", "AA:BB:CC:DD:EE", "", "AA-BB-CC-DD-EE-FF"])
    def test_rejects_other_things(self, value):
        assert not is_address(value)


class TestPersistence:
    def test_missing_file_gives_an_empty_config(self, path):
        config = Config.load(path)
        assert config.lights == {}
        assert config.default is None

    def test_round_trip(self, path):
        config = Config()
        config.add_light("desk", ADDRESS, gamut="c")
        config.add_scene("movie", Scene(color="#ff0000", brightness=20))
        config.save(path)

        reloaded = Config.load(path)
        assert reloaded.lights["desk"].address == ADDRESS
        assert reloaded.lights["desk"].gamut == "C"
        assert reloaded.scenes["movie"].brightness == 20
        assert reloaded.default == "desk"

    def test_corrupt_file_is_reported_clearly(self, path):
        path.write_text("{not json")
        with pytest.raises(ConfigError):
            Config.load(path)

    def test_save_leaves_no_temp_file_behind(self, path):
        config = Config()
        config.add_light("desk", ADDRESS)
        config.save(path)
        assert list(path.parent.glob("*.tmp")) == []
        assert json.loads(path.read_text())["default"] == "desk"


class TestLights:
    def test_first_light_becomes_the_default(self):
        config = Config()
        config.add_light("desk", ADDRESS)
        config.add_light("lamp", OTHER)
        assert config.default == "desk"

    def test_address_is_normalised(self):
        config = Config()
        config.add_light("desk", ADDRESS.lower())
        assert config.lights["desk"].address == ADDRESS

    def test_alias_cannot_be_an_address(self):
        with pytest.raises(ConfigError):
            Config().add_light(ADDRESS, ADDRESS)

    def test_address_must_look_like_one(self):
        with pytest.raises(ConfigError):
            Config().add_light("desk", "nope")

    def test_removing_the_default_promotes_another(self):
        config = Config()
        config.add_light("desk", ADDRESS)
        config.add_light("lamp", OTHER)
        config.remove_light("desk")
        assert config.default == "lamp"

    def test_removing_the_last_light_clears_the_default(self):
        config = Config()
        config.add_light("desk", ADDRESS)
        config.remove_light("desk")
        assert config.default is None

    def test_removing_an_unknown_light_is_an_error(self):
        with pytest.raises(ConfigError):
            Config().remove_light("ghost")


class TestResolve:
    @pytest.fixture
    def config(self):
        config = Config()
        config.add_light("desk", ADDRESS)
        config.add_light("lamp", OTHER)
        return config

    def test_default_is_used_when_nothing_is_given(self, config):
        assert [alias for alias, _ in config.resolve(None)] == ["desk"]

    def test_alias(self, config):
        assert config.resolve("lamp")[0][1].address == OTHER

    def test_all(self, config):
        assert len(config.resolve("all")) == 2

    def test_comma_separated_list(self, config):
        assert [alias for alias, _ in config.resolve("desk,lamp")] == ["desk", "lamp"]

    def test_bare_address_needs_no_alias(self, config):
        alias, light = config.resolve("00:11:22:33:44:55")[0]
        assert alias is None
        assert light.address == "00:11:22:33:44:55"

    def test_unknown_alias_is_an_error(self, config):
        with pytest.raises(ConfigError):
            config.resolve("kitchen")

    def test_single_saved_light_needs_no_default(self):
        config = Config()
        config.add_light("desk", ADDRESS)
        config.default = None
        assert config.resolve(None)[0][1].address == ADDRESS

    def test_ambiguous_selection_asks_for_one(self, config):
        config.default = None
        with pytest.raises(ConfigError):
            config.resolve(None)

    def test_no_lights_at_all_explains_the_setup(self):
        with pytest.raises(ConfigError) as caught:
            Config().resolve(None)
        assert "scan" in (caught.value.hint or "")


class TestScenes:
    def test_add_and_remove(self):
        config = Config()
        config.add_scene("movie", Scene(color="red", brightness=15))
        assert "movie" in config.scenes
        config.remove_scene("movie")
        assert "movie" not in config.scenes

    def test_removing_an_unknown_scene_is_an_error(self):
        with pytest.raises(ConfigError):
            Config().remove_scene("ghost")
