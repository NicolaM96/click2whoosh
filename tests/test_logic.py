"""Tests of the logic that needs neither the Click nor MyWhoosh.

    .venv/bin/python -m unittest discover tests
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# The code lives in src/: make it importable from here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app
import ble
import i18n
import obc
from actions import Action
from ble import ClickEvents, ClickSide, SIDE_LEFT, SIDE_RIGHT, pressed_buttons


def setUpModule():
    i18n.set_language("en")  # the expected texts below are English whatever the Mac's language is


def varint(value: int) -> bytes:
    out = b""
    while True:
        byte, value = value & 0x7F, value >> 7
        out += bytes([byte | (0x80 if value else 0)])
        if not value:
            return out


def keypad(*masks: int) -> bytes:
    button_map = 0xFFFFFFFF
    for mask in masks:
        button_map &= ~mask
    return bytes([0x23, 0x08]) + varint(button_map)


class DecodeTest(unittest.TestCase):
    def test_frames_from_the_real_click(self):
        # Bytes copied from the Click v2 logs (firmware 1.1.0).
        self.assertEqual(pressed_buttons(bytes.fromhex("08ffffffff0f")), set())
        self.assertEqual(pressed_buttons(bytes.fromhex("08dfffffff0f")), {"b"})
        self.assertEqual(pressed_buttons(bytes.fromhex("08efffffff0f")), {"a"})
        self.assertEqual(pressed_buttons(bytes.fromhex("08bfffffff0f")), {"y"})
        self.assertEqual(pressed_buttons(bytes.fromhex("08fffeffff0f")), {"z"})
        self.assertEqual(pressed_buttons(bytes.fromhex("08ffdfffff0f")), {"plus"})

    def test_press_and_release_events(self):
        events = []
        side = ClickSide(SIDE_RIGHT, ClickEvents(on_press=lambda n: events.append(("▼", n)),
                                                 on_release=lambda n: events.append(("▲", n))))
        side.handle(keypad(0x01000))
        side.handle(keypad(0x01000))  # the Click repeats the same state: no new event
        side.handle(keypad())
        self.assertEqual(events, [("▼", "plus"), ("▲", "plus")])

    def test_each_click_keeps_only_its_buttons(self):
        # The left Click also forwards the right one's buttons: Y must not arrive twice.
        events = []
        left = ClickSide(SIDE_LEFT, ClickEvents(on_press=lambda n: events.append(n)))
        left.handle(keypad(0x00040 | 0x00100))  # Y (right) and − (left) together
        self.assertEqual(events, ["minus"])

    def test_garbage_does_not_crash(self):
        side = ClickSide(SIDE_RIGHT, ClickEvents())
        side.handle(bytes([0x23, 0x08, 0xFF]))  # truncated varint
        side.handle(b"")


class FakeAction(app.Action):
    def __init__(self, spec, log):
        super().__init__(spec)
        self.log = log

    def press(self):
        self.log.append(("press", self.spec))

    def release(self):
        self.log.append(("release", self.spec))


class DispatcherTest(unittest.TestCase):
    def make(self, **general):
        config = app.Config(app.DEFAULT_CONFIG)
        self.log = []
        config.buttons = {"plus": FakeAction("key:k", self.log), "z": FakeAction("hold:a", self.log),
                          "y": FakeAction("media:next", self.log)}
        config.long_press = {"y": FakeAction("media:playpause", self.log)}
        config.double_press = {}
        config.long_press_s = 0.1
        config.double_window_s = 0.1
        for key, value in general.items():
            setattr(config, key, value)
        return app.Dispatcher(config)

    def test_keys_only_reach_mywhoosh(self):
        d = self.make()
        with mock.patch.object(app, "frontmost_app", return_value="Safari"):
            d.on_press("plus")
            d.on_press("y"); d.on_release("y")  # music always plays
        self.assertEqual(self.log, [("press", "media:next")])
        with mock.patch.object(app, "frontmost_app", return_value="MyWhoosh Indoor Cycling App"):
            d.on_press("plus"); d.on_release("plus")
        self.assertIn(("press", "key:k"), self.log)

    def test_hold_is_released_even_if_mywhoosh_loses_focus(self):
        d = self.make()
        with mock.patch.object(app, "frontmost_app", return_value="MyWhoosh"):
            d.on_press("z")
        with mock.patch.object(app, "frontmost_app", return_value="Safari"):
            d.on_release("z")
        self.assertEqual(self.log, [("press", "hold:a"), ("release", "hold:a")])

    def test_short_and_long_press(self):
        d = self.make()
        d.on_press("y"); d.on_release("y")
        d.on_press("y"); time.sleep(0.2); d.on_release("y")
        self.assertEqual(self.log, [("press", "media:next"), ("press", "media:playpause")])

    def test_single_double_and_long_press(self):
        d = self.make()
        d.config.double_press = {"y": FakeAction("media:previous", self.log)}
        d.on_press("y"); d.on_release("y"); time.sleep(0.2)          # single: after the wait
        d.on_press("y"); d.on_release("y"); d.on_press("y"); d.on_release("y"); time.sleep(0.2)  # double
        d.on_press("y"); time.sleep(0.2); d.on_release("y")          # long
        self.assertEqual(self.log, [("press", "media:next"), ("press", "media:previous"),
                                    ("press", "media:playpause")])

    def test_buttons_only_select_while_settings_has_focus(self):
        d = self.make()
        seen = []
        d.listener = lambda name, pressed: seen.append((name, pressed))
        d.capture = lambda: True
        d.on_press("y"); d.on_release("y"); time.sleep(0.2)
        self.assertEqual(self.log, [])  # no music
        self.assertEqual(seen, [("y", True), ("y", False)])  # but the button lights up
        d.capture = lambda: False
        d.on_press("y"); d.on_release("y"); time.sleep(0.2)
        self.assertEqual(self.log, [("press", "media:next")])

    def test_sound_on_shift_only(self):
        config = app.Config(app.DEFAULT_CONFIG)
        d = app.Dispatcher(config)
        with mock.patch.object(app, "play_shift_sound") as sound, \
                mock.patch.object(obc.OBCServer, "connected", new_callable=mock.PropertyMock, return_value=True), \
                mock.patch.object(obc.server, "send"), mock.patch("actions.post_media_key"):
            d.on_press("plus"); d.on_release("plus")
            d.on_press("minus"); d.on_release("minus")
            d.on_press("nav_left"); d.on_release("nav_left")  # steering makes no sound
            self.assertEqual([c.args for c in sound.call_args_list], [("up",), ("down",)])
            d.config.shift_sound = False
            d.on_press("plus"); d.on_release("plus")
            self.assertEqual(sound.call_count, 2)

    def test_holding_plus_repeats(self):
        d = self.make()
        self.enterContext(mock.patch.multiple(app, REPEAT_DELAY_S=0.1, REPEAT_INTERVAL_S=0.05))
        with mock.patch.object(app, "frontmost_app", return_value="MyWhoosh"):
            d.on_press("plus"); time.sleep(0.3); d.on_release("plus")
            count = self.log.count(("press", "key:k"))
            time.sleep(0.2)  # after the release it must not repeat any more
            self.assertEqual(self.log.count(("press", "key:k")), count)
        self.assertGreaterEqual(count, 3)

class KeepAwakeTest(unittest.TestCase):
    def test_sleeps_when_idle_or_mywhoosh_closed(self):
        config = app.Config(app.DEFAULT_CONFIG)
        bridge = app.ClickBridge(app.Dispatcher(config), config)
        with mock.patch.object(app, "mywhoosh_running", return_value=True):
            with mock.patch.object(app, "frontmost_app", return_value="Safari"):
                self.assertTrue(bridge.keep_awake_allowed())
                bridge.dispatcher.last_press -= 11 * 60
                self.assertFalse(bridge.keep_awake_allowed())  # MyWhoosh behind: 10 minutes
            with mock.patch.object(app, "frontmost_app", return_value="MyWhoosh Indoor Cycling App"):
                self.assertTrue(bridge.keep_awake_allowed())  # MyWhoosh in front: up to 60 minutes
                bridge.dispatcher.last_press -= 50 * 60
                self.assertFalse(bridge.keep_awake_allowed())
        bridge.dispatcher.last_press = time.monotonic()
        with mock.patch.object(app, "mywhoosh_running", return_value=False):
            self.assertTrue(bridge.keep_awake_allowed())  # button just pressed: stays awake
            bridge.dispatcher.last_press -= 3 * 60
            self.assertFalse(bridge.keep_awake_allowed())

    def test_reset_only_when_allowed(self):
        class FakeClient:
            def __init__(self):
                self.writes = []

            async def write_gatt_char(self, uuid, data, response):
                self.writes.append(bytes(data))

        for allowed, expected in ((True, [bytes([0x18])]), (False, [])):
            client = FakeClient()
            side = ClickSide(SIDE_RIGHT, ClickEvents(keep_awake_allowed=lambda a=allowed: a),
                             keepalive="reset", keepalive_interval=1)
            side.last_message = time.monotonic()
            asyncio.run(side.keep_awake(client))
            self.assertEqual(client.writes, expected)
            self.assertEqual(side.just_reset, allowed)

    def test_wake_sequence_matches_bikecontrol(self):
        import ble

        class Char:
            def __init__(self, uuid):
                self.uuid, self.obj = uuid, None

        class FakeClient:
            def __init__(self):
                self.writes = []

            async def write_gatt_char(self, char, data, response):
                self.writes.append((char.uuid[4:8], bytes(data).hex(" ")))

            async def read_gatt_char(self, uuid):
                return bytearray([57])

        client = FakeClient()
        side = ClickSide(SIDE_RIGHT, ClickEvents())
        asyncio.run(side.wake_sequence(client, Char(ble.WAKE_CHAR_UUID), Char(ble.WAKE_CONTROL_UUID)))
        # The same bytes BikeControl sends to the Click (PacketLogger capture).
        self.assertEqual(client.writes, [
            ("0102", "01"),
            ("0100", "3d b8 f3 96 00 00 00 00 00 01 00 00 00 b0 03 00 00 00"),
            ("0102", "03"),
        ])

    def test_power_off_all_from_another_thread(self):
        import threading
        config = app.Config(app.DEFAULT_CONFIG)
        bridge = app.ClickBridge(app.Dispatcher(config), config)
        calls = []

        class FakeSide:
            async def power_off(self, reason):
                calls.append(reason)

        bridge.sides = [FakeSide()]
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        bridge.loop = loop
        bridge.power_off_all("prova")  # as Quit and the Mac's sleep do, from the main thread
        loop.call_soon_threadsafe(loop.stop)
        self.assertEqual(calls, ["prova"])

    def test_wakes_later_if_a_button_is_pressed_after_connecting(self):
        import ble

        class Char:
            def __init__(self, uuid):
                self.uuid, self.obj = uuid, None

        class Services:
            def get_characteristic(self, uuid):
                return Char(uuid)

        class FakeClient:
            services = Services()

            def __init__(self):
                self.writes = []

            async def write_gatt_char(self, char, data, response):
                self.writes.append(bytes(data))

            async def read_gatt_char(self, uuid):
                return bytearray([80])

        answers = iter([False, True])  # just connected it is not needed; then a button is pressed
        side = ClickSide(SIDE_RIGHT, ClickEvents(keep_awake_allowed=lambda: next(answers, True)),
                         keepalive="wake", keepalive_interval=600)
        client = FakeClient()

        async def run():
            try:
                await asyncio.wait_for(side.keep_awake(client), timeout=3)
            except asyncio.TimeoutError:
                pass

        asyncio.run(run())
        self.assertIn(ble.WAKE_MESSAGE, client.writes)

    def test_held_button_is_released_if_the_connection_fails(self):
        released = []
        side = ClickSide(SIDE_RIGHT, ClickEvents(on_release=released.append))
        side.pressed = {"z"}
        side.device, side.just_reset = object(), True

        async def boom(device):
            raise OSError("connessione persa")

        side.connect_and_listen = boom
        with self.assertRaises(OSError):
            asyncio.run(side.run_once())
        self.assertEqual(released, ["z"])

    def test_no_keep_awake_while_mac_sleeps(self):
        config = app.Config(app.DEFAULT_CONFIG)
        bridge = app.ClickBridge(app.Dispatcher(config), config)
        bridge.mac_sleeping = True
        self.assertFalse(bridge.keep_awake_allowed())

    def test_light_search_without_mywhoosh(self):
        side = ClickSide(SIDE_RIGHT, ClickEvents(search_often=lambda: False))
        side.find = mock.AsyncMock(return_value=None)

        async def run():
            with mock.patch("asyncio.sleep", mock.AsyncMock()) as sleep:
                await side.run_once()
                return sleep

        sleep = asyncio.run(run())
        side.find.assert_awaited_once_with(timeout=8)  # search...
        sleep.assert_awaited_once_with(4)              # ...and a short pause

    def test_search_often_follows_mywhoosh(self):
        config = app.Config(app.DEFAULT_CONFIG)
        bridge = app.ClickBridge(app.Dispatcher(config), config)
        with mock.patch.object(app, "mywhoosh_running", return_value=False):
            self.assertFalse(bridge.search_often())
        with mock.patch.object(app, "mywhoosh_running", return_value=True):
            self.assertTrue(bridge.search_often())
            bridge.mac_sleeping = True
            self.assertFalse(bridge.search_often())

    def test_warns_once_if_firmware_changes(self):
        config = app.Config(app.DEFAULT_CONFIG)
        bridge = app.ClickBridge(app.Dispatcher(config), config, use_bluetooth=False)
        with mock.patch.object(app, "notify") as notify:
            bridge.on_firmware("right", "1.1.0")
            bridge.on_firmware("right", "1.2.0")
            bridge.on_firmware("right", "1.2.0")
        self.assertEqual(notify.call_count, 1)

    def test_battery_only_while_connected(self):
        config = app.Config(app.DEFAULT_CONFIG)
        bridge = app.ClickBridge(app.Dispatcher(config), config, use_bluetooth=False)
        menu = mock.Mock(bridge=bridge)
        bridge.battery["right"] = 60
        bridge.status["right"] = ble.STATUS_CONNECTED
        bridge.battery["left"] = 90
        bridge.status["left"] = ble.STATUS_CONNECTED
        self.assertEqual(app.MenuBarApp.batteries(menu), {"right": 60, "left": 90})
        bridge.status["right"] = ble.STATUS_SEARCHING  # disconnected: the 60% would be stale data
        self.assertEqual(app.MenuBarApp.batteries(menu), {"left": 90})

    def test_sleep_reason_logged_once(self):
        config = app.Config(app.DEFAULT_CONFIG)
        bridge = app.ClickBridge(app.Dispatcher(config), config)
        bridge.dispatcher.last_press -= 3 * 60
        with mock.patch.object(app, "mywhoosh_running", return_value=False), \
                self.assertLogs("click2whoosh", level="INFO") as logs:
            for _ in range(5):
                self.assertFalse(bridge.keep_awake_allowed())
        self.assertEqual(len(logs.output), 1)  # no spam: a single line

    def test_no_search_right_after_power_off(self):
        side = ClickSide(SIDE_RIGHT, ClickEvents())
        side.rest_until = time.monotonic() + 60
        side.run_once = mock.AsyncMock()

        async def run():
            try:
                await asyncio.wait_for(side.run_forever(), timeout=0.3)
            except asyncio.TimeoutError:
                pass

        asyncio.run(run())
        side.run_once.assert_not_called()

    def test_system_watcher(self):
        config = app.Config(app.DEFAULT_CONFIG)
        bridge = app.ClickBridge(app.Dispatcher(config), config, use_bluetooth=False)
        bridge.power_off_all = mock.Mock()
        with mock.patch.object(app.rumps.events.on_sleep, "register"), \
                mock.patch.object(app.rumps.events.on_wake, "register"):
            republish = mock.Mock()
            watcher = app.SystemWatcher(bridge, bridge.dispatcher, republish)
        watcher.mac_sleeping()
        self.assertTrue(bridge.mac_sleeping)
        bridge.power_off_all.assert_called_once()
        watcher.mac_woke()
        self.assertFalse(bridge.mac_sleeping)
        republish.assert_called_once()  # on wake the announcement for MyWhoosh is made again
        with mock.patch.object(app, "notify") as notify, \
                mock.patch.object(obc.OBCServer, "connected", new_callable=mock.PropertyMock, return_value=False):
            with mock.patch.object(app, "mywhoosh_running", return_value=False):
                watcher.check_lost()  # MyWhoosh closed by you: no warning
            notify.assert_not_called()
            with mock.patch.object(app, "mywhoosh_running", return_value=True):
                watcher.check_lost()  # open but disconnected from the network: warning
            notify.assert_called_once()

    def test_simple_status(self):
        # Restarting and connected look the same, as do searching and disconnected.
        self.assertEqual(app.simple_status(ble.STATUS_RESTARTING), ble.STATUS_CONNECTED)
        self.assertEqual(app.simple_status(ble.STATUS_SEARCHING), app.STATUS_WAITING)
        self.assertEqual(app.simple_status(ble.STATUS_DISCONNECTED), app.STATUS_WAITING)
        self.assertEqual(app.simple_status(ble.STATUS_BLUETOOTH_OFF), ble.STATUS_BLUETOOTH_OFF)

    def test_status_text(self):
        self.assertEqual(app.status_text(ble.STATUS_CONNECTED), "connected")
        self.assertEqual(app.status_text(app.STATUS_WAITING), "waiting (press a button)")
        self.assertEqual(app.status_text(ble.STATUS_BLUETOOTH_OFF), "Bluetooth off")

    def test_invalid_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for text in ('[buttons]\nplus = "key:ç"', '[buttons]\nplus = "boh"',
                         '[general]\nlong_press_ms = 0', '[general]\ndouble_press_ms = 5000',
                         '[general]\nbattery_warning = 150'):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    app.Config(path)


if __name__ == "__main__":
    unittest.main()


class OpenBikeControlTest(unittest.TestCase):
    def test_app_info_and_button_messages(self):
        async def scenario():
            server = obc.OBCServer()
            server.on_change = changes.append
            with mock.patch.object(obc, "PREFERRED_PORT", 46867):
                await server.start()
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            status = await reader.readexactly(3)
            app_id, version = b"mywhoosh", b"5.1"
            writer.write(bytes([0x04, 0x01, len(app_id)]) + app_id + bytes([len(version)]) + version
                         + bytes([3, 0x01, 0x02, 0x40]))
            await writer.drain()
            await asyncio.sleep(0.1)
            server.send([obc.BUTTONS["camera"]], True)
            server.send([obc.BUTTONS["camera"]], False)
            server.send([obc.BUTTONS["shift_up"], obc.BUTTONS["select"]], True)
            messages = await reader.readexactly(11)
            info = server.app
            writer.close()
            await asyncio.sleep(0.1)  # the server notices the closing
            await server.stop()
            return status, info, messages

        changes = []
        status, info, messages = asyncio.run(scenario())
        self.assertEqual(changes, [True, False])  # connected, then disconnected
        self.assertEqual(status, bytes([0x02, 0xFF, 0x01]))
        self.assertEqual((info.app_id, info.version, info.buttons), ("mywhoosh", "5.1", [0x01, 0x02, 0x40]))
        self.assertEqual(messages, bytes([0x01, 0x40, 0x01, 0x01, 0x40, 0x00,
                                          0x01, 0x01, 0x01, 0x14, 0x01]))  # shift up + confirm together

    def test_combined_command_falls_back_to_first(self):
        combo = Action("obc:shift_up+select")
        self.assertEqual(combo.button_ids, [0x01, 0x14])
        self.assertEqual(combo.fallback.spec, "key:k")
        with self.assertRaises(ValueError):
            Action("obc:shift_up+boh")

    def test_heartbeat_is_ignored_quietly(self):
        server = obc.OBCServer()
        with self.assertNoLogs("click2whoosh", level="INFO"):
            self.assertEqual(server.consume(b"\x00\x00"), b"")

    def test_partial_app_info_waits_for_the_rest(self):
        message = bytes([0x04, 0x01, 0x02]) + b"mw" + bytes([0x01]) + b"1" + bytes([0x01, 0x01])
        self.assertEqual(obc.parse_app_info(message[:6]), (None, 0))
        info, used = obc.parse_app_info(message)
        self.assertEqual((info.app_id, used), ("mw", len(message)))

    def test_falls_back_to_keyboard_when_not_connected(self):
        shift = Action("obc:shift_up")
        camera = Action("obc:camera")
        with mock.patch.object(obc.OBCServer, "connected", new_callable=mock.PropertyMock, return_value=False), \
                mock.patch("actions.post_key") as post_key, mock.patch.object(obc.server, "send") as send:
            self.assertTrue(shift.needs_target_app)
            self.assertFalse(camera.needs_target_app)  # no equivalent key: it only goes over the network
            shift.press(); shift.release()
            self.assertEqual([c.args for c in post_key.call_args_list], [(40, True), (40, False)])
            send.assert_not_called()
        with mock.patch.object(obc.OBCServer, "connected", new_callable=mock.PropertyMock, return_value=True), \
                mock.patch("actions.post_key") as post_key, mock.patch.object(obc.server, "send") as send:
            self.assertFalse(shift.needs_target_app)
            shift.press(); shift.release()
            self.assertEqual([c.args for c in send.call_args_list], [([0x01], True), ([0x01], False)])
            post_key.assert_not_called()


class SettingsTest(unittest.TestCase):
    def raw(self):
        with open(app.DEFAULT_CONFIG, "rb") as f:
            import tomllib
            return tomllib.load(f)

    def test_template_matches_default_buttons(self):
        from actions import DEFAULT_BUTTONS
        raw = self.raw()
        self.assertEqual(raw["buttons"], DEFAULT_BUTTONS[True]["buttons"])
        self.assertEqual(raw["general"]["repeat_buttons"], DEFAULT_BUTTONS[True]["repeat"])

    def test_saved_file_is_valid_and_keeps_everything(self):
        import settings
        raw = self.raw()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(settings.dump_config(raw))
            config = app.Config(path)
        self.assertEqual(config.buttons["plus"].spec, "obc:shift_up")
        self.assertEqual(config.double_press["y"].spec, "media:next")
        self.assertEqual(config.repeat, {"plus", "minus", "nav_up", "nav_down"})

    def test_default_buttons_follow_left_click(self):
        import settings
        from actions import DEFAULT_BUTTONS
        raw = self.raw()
        self.assertTrue(settings.switch_default_buttons(raw, False))
        self.assertEqual(raw["buttons"], DEFAULT_BUTTONS[False]["buttons"])
        self.assertEqual(raw["buttons"]["b"], "obc:shift_down")
        self.assertTrue(settings.switch_default_buttons(raw, True))
        self.assertEqual(raw["buttons"]["nav_up"], "media:volup")
        self.assertIn("nav_up", raw["general"]["repeat_buttons"])
        # buttons changed by hand: not touched
        settings.apply_button_change(raw, "a", "single", "key:3")
        self.assertFalse(settings.switch_default_buttons(raw, False))
        self.assertEqual(raw["buttons"]["a"], "key:3")

    def test_old_untouched_config_is_updated_at_startup(self):
        import settings
        from actions import DEFAULT_BUTTONS
        raw = self.raw()
        settings.switch_default_buttons(raw, False)  # like a config created with the right Click's buttons only
        raw["general"]["use_left_side"] = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(settings.dump_config(raw))
            settings.update_default_buttons(path)
            self.assertEqual(app.Config(path).buttons["nav_up"].spec, "media:volup")
            # customized: not touched
            raw["buttons"]["y"] = "media:next"
            path.write_text(settings.dump_config(raw))
            settings.update_default_buttons(path)
            self.assertEqual(app.Config(path).buttons["y"].spec, "media:next")
            self.assertEqual(app.Config(path).buttons["nav_up"].spec, DEFAULT_BUTTONS[False]["buttons"]["nav_up"])

    def test_volume_repeats(self):
        from actions import repeatable
        self.assertTrue(repeatable("media:volup"))
        self.assertFalse(repeatable("media:playpause"))

    def test_button_changes(self):
        import settings
        raw = self.raw()
        self.assertEqual(settings.button_state(raw, "plus")["long"], "repeat")
        settings.apply_button_change(raw, "plus", "long", "media:playpause")
        self.assertNotIn("plus", raw["general"]["repeat_buttons"])
        self.assertEqual(settings.button_state(raw, "plus")["long"], "media:playpause")
        settings.apply_button_change(raw, "plus", "long", "repeat")
        self.assertIn("plus", raw["general"]["repeat_buttons"])
        self.assertNotIn("plus", raw["long_press"])
        # Steering cannot have a double click or a long press.
        settings.apply_button_change(raw, "y", "single", "obc:steer_left")
        self.assertNotIn("y", raw["long_press"])
        self.assertNotIn("y", raw["double_press"])

    def test_button_summary_for_welcome_window(self):
        import settings
        keys = {k["name"]: k for k in settings.button_summary(self.raw())}
        self.assertEqual(list(keys), ["plus", "y", "z", "a", "b"])
        self.assertEqual((keys["plus"]["single"], keys["plus"]["long"]), ("Shift up", "repeats"))
        self.assertEqual((keys["y"]["double"], keys["y"]["long"]), ("Next track", "Previous track"))
        self.assertEqual(keys["z"]["single"], "U key (minimal interface)")
        self.assertEqual({k["side"] for k in keys.values()}, {"right"})
        left = {k["name"]: k for k in settings.button_summary(self.raw(), left=True) if k["side"] == "left"}
        self.assertEqual(list(left), ["minus", "nav_up", "nav_down", "nav_left", "nav_right"])
        self.assertTrue(left["nav_left"]["held"])
        self.assertEqual(left["nav_up"]["long"], "repeats")
