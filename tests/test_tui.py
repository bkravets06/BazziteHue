"""Tests for the TUI's pure logic: key handling, the model, and rendering helpers.

Nothing here initialises curses, so the suite still runs without a terminal.
"""

import curses

import pytest
from test_light import FakeClient, reset_fake_client

from bazzitehue import light as light_module
from bazzitehue import protocol
from bazzitehue.light import HueLight
from bazzitehue.tui import Model, _ansi256, _apply_key, _bar, run_tui


class TestModel:
    def test_colour_mode_tracks_the_hue(self):
        red = Model(hue=0, saturation=100).rgb()
        green = Model(hue=120, saturation=100).rgb()
        assert red.index(max(red)) == 0
        assert green.index(max(green)) == 1

    def test_white_mode_uses_the_temperature(self):
        warm = Model(white_mode=True, kelvin=2000).rgb()
        cool = Model(white_mode=True, kelvin=6500).rgb()
        assert warm[2] < cool[2]

    def test_xy_is_inside_the_unit_square(self):
        x, y = Model(hue=200, saturation=80).xy()
        assert 0 <= x <= 1 and 0 <= y <= 1

    def test_a_snapshot_is_independent(self):
        model = Model()
        snapshot = Model(**vars(model))
        model.brightness = 5
        assert snapshot.brightness != 5


class TestKeys:
    def test_arrows_move_hue(self):
        model = Model(hue=100)
        assert _apply_key(curses.KEY_RIGHT, model)[0] is True
        assert model.hue == 105
        _apply_key(curses.KEY_LEFT, model)
        assert model.hue == 100

    def test_hue_wraps_around_the_wheel(self):
        model = Model(hue=2)
        _apply_key(curses.KEY_LEFT, model)
        assert model.hue == 357

    def test_shifted_keys_move_further(self):
        model = Model(hue=0)
        _apply_key(ord("L"), model)
        assert model.hue == 20

    def test_brightness_is_clamped(self):
        model = Model(brightness=98)
        _apply_key(curses.KEY_UP, model)
        assert model.brightness == 100
        model.brightness = 2
        _apply_key(curses.KEY_DOWN, model)
        assert model.brightness == 0

    def test_temperature_keys_switch_to_white_mode(self):
        model = Model()
        _apply_key(ord("t"), model)
        assert model.white_mode is True
        assert model.kelvin == 2600

    def test_temperature_is_clamped_to_what_bulbs_support(self):
        model = Model(kelvin=2050, white_mode=True)
        _apply_key(ord("t"), model)
        assert model.kelvin == 2000
        model.kelvin = 6450
        _apply_key(ord("T"), model)
        assert model.kelvin == 6500

    def test_colour_keys_leave_white_mode(self):
        model = Model(white_mode=True)
        _apply_key(curses.KEY_RIGHT, model)
        assert model.white_mode is False

    def test_space_toggles_power(self):
        model = Model(power=True)
        changed, message = _apply_key(ord(" "), model)
        assert changed and model.power is False
        assert "power" in message

    def test_presets_set_hue_and_saturation(self):
        model = Model(white_mode=True)
        changed, message = _apply_key(ord("4"), model)
        assert changed
        assert model.white_mode is False
        assert model.saturation == 100
        assert "green" in message

    def test_unknown_keys_change_nothing(self):
        model = Model()
        before = vars(model).copy()
        assert _apply_key(ord("z"), model) == (False, "")
        assert vars(model) == before


class TestRendering:
    @pytest.mark.parametrize(
        "rgb,expected",
        [((0, 0, 0), 16), ((255, 255, 255), 231), ((255, 0, 0), 196)],
    )
    def test_ansi256_cube(self, rgb, expected):
        assert _ansi256(rgb) == expected

    def test_ansi256_stays_in_range(self):
        for value in (-50, 0, 128, 999):
            index = _ansi256((value, value, value))
            assert 16 <= index <= 231

    def test_bar_endpoints(self):
        assert _bar(0, width=10) == "·" * 10
        assert _bar(100, width=10) == "█" * 10
        assert len(_bar(37, width=10)) == 10


class FakeScreen:
    """Enough of a curses window to drive run_tui's input loop.

    Like a real window with a timeout set, getch() returns -1 between key
    presses, so the loop gets a chance to push and redraw between them.
    """

    def __init__(self, keys):
        self.keys = [item for key in keys for item in (key, -1)]
        self.lines: list[str] = []

    def getch(self):
        return self.keys.pop(0) if self.keys else -1

    def getmaxyx(self):
        return (30, 80)

    def addstr(self, _row, _col, text, _attr=0):
        self.lines.append(text)

    def erase(self):
        self.lines.clear()

    def noutrefresh(self):
        pass

    def keypad(self, _flag):
        pass

    def timeout(self, _ms):
        pass


@pytest.fixture
def headless_curses(monkeypatch):
    """Stub out the parts of curses that need a real terminal."""
    screens: list[FakeScreen] = []

    def initscr():
        return screens[0]

    for name, stub in [
        ("initscr", initscr),
        ("noecho", lambda: None),
        ("echo", lambda: None),
        ("cbreak", lambda: None),
        ("nocbreak", lambda: None),
        ("curs_set", lambda _n: None),
        ("endwin", lambda: None),
        ("doupdate", lambda: None),
        ("has_colors", lambda: False),
        ("start_color", lambda: None),
    ]:
        monkeypatch.setattr(curses, name, stub)

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    return screens


class TestRunLoop:
    """The loop that reads keys and pushes changes to the lamp."""

    @pytest.fixture
    def light(self, monkeypatch):
        reset_fake_client()
        monkeypatch.setattr(light_module, "BleakClient", FakeClient)

        async def fake_resolve(address, timeout=8.0):
            return address

        monkeypatch.setattr(light_module, "resolve_device", fake_resolve)
        return HueLight("AA:BB:CC:DD:EE:FF", alias="desk")

    async def run(self, headless_curses, light, keys):
        headless_curses.append(FakeScreen([*keys, ord("q")]))
        code = await run_tui([light])
        assert code == 0
        return FakeClient.stores["AA:BB:CC:DD:EE:FF"]

    async def test_q_quits_and_disconnects(self, headless_curses, light):
        await self.run(headless_curses, light, [])
        assert not light.is_connected

    async def test_a_bare_escape_does_not_quit(self, headless_curses, light):
        # With a short getch timeout an unfinished escape sequence arrives as a
        # lone ESC; quitting on it would end the session on any arrow key.
        store = await self.run(headless_curses, light, [27, curses.KEY_RIGHT, 27])
        assert protocol.decode_xy(store[protocol.CHAR_COLOR]) != pytest.approx((0.4, 0.4))

    async def test_arrow_keys_reach_the_lamp(self, headless_curses, light):
        store = await self.run(headless_curses, light, [curses.KEY_DOWN] * 4)
        # Read-back starts at 100%; four presses take 20% off.
        assert protocol.raw_to_percent(store[protocol.CHAR_BRIGHTNESS][0]) == pytest.approx(
            80, abs=1
        )

    async def test_presets_reach_the_lamp(self, headless_curses, light):
        store = await self.run(headless_curses, light, [ord("6")])  # blue
        x, y = protocol.decode_xy(store[protocol.CHAR_COLOR])
        assert x < 0.25 and y < 0.25

    async def test_white_mode_writes_a_temperature(self, headless_curses, light):
        store = await self.run(headless_curses, light, [ord("T")] * 3)
        assert protocol.decode_mired(store[protocol.CHAR_TEMPERATURE]) != 370

    async def test_power_toggle(self, headless_curses, light):
        store = await self.run(headless_curses, light, [ord(" ")])
        assert store[protocol.CHAR_POWER] == b"\x00"

    async def test_a_write_failure_is_shown_not_swallowed(self, headless_curses, light):
        screen = FakeScreen([curses.KEY_RIGHT, curses.KEY_RIGHT, ord("q")])
        headless_curses.append(screen)
        FakeClient.auth_error = True

        assert await run_tui([light]) == 0
        assert any("refused" in line for line in screen.lines)

    async def test_it_survives_a_lamp_that_will_not_answer_reads(
        self, headless_curses, light, monkeypatch
    ):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("no reads for you")

        monkeypatch.setattr(HueLight, "get_state", boom)
        screen = FakeScreen([ord("q")])
        headless_curses.append(screen)
        assert await run_tui([light]) == 0

    async def test_it_refuses_to_start_without_a_terminal(self, monkeypatch, light, capsys):
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        assert await run_tui([light]) == 2
        assert "interactive terminal" in capsys.readouterr().err
