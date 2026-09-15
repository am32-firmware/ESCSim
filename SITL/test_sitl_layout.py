"""Laptop layout checks with real Qt widgets and no running ESC process."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QRect
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTabWidget
from sitl_gui import EscFleet
from sitl_layout import fit_window, tile_windows
from sitl_scope import ScopeCapture, ScopeFrame
from sitl_scope_ui import DemagScopeWindow
from test_sitl_scope import sample


class LaptopLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.original_font = cls.app.font()
        # GitHub's Linux runner resolves Sans Serif to DejaVu Sans. Its
        # wider glyphs exposed channel panels that could not tile at 150%.
        cls.app.setFont(QFont('DejaVu Sans', 9))

    @classmethod
    def tearDownClass(cls):
        cls.app.setFont(cls.original_font)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sitl-layout-')
        eeprom = Path(self.tmp.name) / 'eeprom.bin'
        eeprom.write_bytes(bytes(256))
        args = SimpleNamespace(host='127.0.0.1', port=29733, state_port=29734,
                               can_uri='mcast:245', poles=14, control_port=0,
                               log=None, replay=None, esc_count=1)
        # Render the full CAN controls without starting a DroneCAN worker.
        rate = SimpleNamespace(hz=lambda: 0)
        can = SimpleNamespace(started=SimpleNamespace(wait=lambda _: True),
            enabled=False, throttle=0, error='', status={}, esc_rate=rate,
            sent=rate, node_id=1, uptime=0, param_result=queue.Queue(),
            thread=SimpleNamespace(join=lambda _: None))
        with patch('sim_runner.bundled_eeprom', return_value=str(eeprom)), \
             patch('sitl_gui.HAVE_DRONECAN', True), \
             patch('sitl_gui.CanPanel', return_value=can):
            self.fleet = EscFleet(args, self.app)
        self.scope = DemagScopeWindow(
            SimpleNamespace(scope=ScopeCapture(), latest=lambda: None),
            lambda: None, lambda: None, controls_window=self.fleet.win)
        self.scope.timer.stop()
        self.fleet.win.show()
        self.scope.show()
        self.app.processEvents()

    def tearDown(self):
        self.scope.close()
        self.fleet.close()
        self.fleet.win.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_pages_fit_and_launch_controls_remain_visible(self):
        # Full live telemetry must wrap, not increase the window minimum.
        for label in self.fleet.win.findChildren(QLabel):
            if label.text().startswith(('BDShot:', 'DroneCAN:')):
                label.setText(label.text() + ' rpm=29522 volt=48.0 current=49.6'
                              ' armed=yes sent=1000/s replies=999/s badcrc=0')
        sections = self.fleet.win.findChild(QTabWidget, 'esc_sections')
        self.assertEqual(sections.count(), 4)
        for width, height in ((660, 950), (520, 780), (500, 650)):
            with self.subTest(size=(width, height)):
                self.fleet.win.resize(width, height)
                for index in range(sections.count()):
                    sections.setCurrentIndex(index)
                    self.app.processEvents()
                    self.assertEqual(self.fleet.win.size().toTuple(), (width, height))
                    page = sections.widget(index)
                    self.assertEqual(page.horizontalScrollBar().maximum(), 0,
                                     sections.tabText(index))
                    for button in self.fleet.win.findChildren(QPushButton):
                        if button.text() in ('Start simulator', 'Stop'):
                            self.assertTrue(button.isVisible())
                            self.assertTrue(self.fleet.win.rect().contains(
                                button.mapTo(self.fleet.win, button.rect().center())))
        # The last file picker remains reachable when its page scrolls.
        sections.setCurrentIndex(3)
        self.app.processEvents()
        page = sections.currentWidget()
        page.ensureWidgetVisible(self.fleet.panels[0].bootloader)
        self.app.processEvents()
        self.assertTrue(page.viewport().rect().contains(
            self.fleet.panels[0].bootloader.mapTo(page.viewport(),
                self.fleet.panels[0].bootloader.rect().center())))

    def test_scope_keeps_plot_and_all_cursor_values_visible(self):
        rows = tuple(sample(t, voltage=48 if (t // 20) % 2 else 0)
                     for t in range(1000))
        self.scope.frame = ScopeFrame(rows, .00025, 'Commutation', .0006, .25)
        self.scope.render()
        for width, height in ((1200, 950), (1000, 780), (1000, 650)):
            with self.subTest(size=(width, height)):
                self.scope.resize(width, height)
                self.scope.inspect_at_us(0)
                self.app.processEvents()
                self.assertEqual(self.scope.size().toTuple(), (width, height))
                self.assertGreater(self.scope.plot.height(), 300)
                self.assertEqual(self.scope.settings_scroll.horizontalScrollBar().maximum(), 0)
                for value in self.scope.channel_values:
                    self.assertTrue(self.scope.rect().contains(
                        value.mapTo(self.scope, value.rect().center())))
                self.assertIn('V', self.scope.channel_values[0].text())
                self.assertIn('A', self.scope.channel_values[1].text())
        scroll = self.scope.settings_scroll
        scroll.ensureWidgetVisible(self.scope.pre)
        self.app.processEvents()
        self.assertTrue(scroll.viewport().rect().contains(
            self.scope.pre.mapTo(scroll.viewport(), self.scope.pre.rect().center())))

    def test_windows_fit_and_tile_at_laptop_scaling(self):
        # 1080p with taskbar space, expressed in Qt logical pixels at
        # 100%, 125% and 150%. Include a nonzero monitor origin.
        for width, height in ((1920, 1000), (1536, 800), (1280, 660)):
            area = QRect(100, 40, width, height)
            screen = SimpleNamespace(availableGeometry=lambda: area)
            with self.subTest(size=(width, height)), \
                 patch.object(self.scope, 'screen', return_value=screen):
                fit_window(self.scope, 1200, 860)
                self.app.processEvents()
                self.assertLessEqual(self.scope.frameGeometry().height(), height)
                self.fleet.win.showMaximized()
                self.scope.showMaximized()
                self.app.processEvents()
                tile_windows(self.fleet.win, self.scope)
                self.app.processEvents()
                left, right = self.fleet.win.frameGeometry(), self.scope.frameGeometry()
                self.assertTrue(area.contains(left), (area, left))
                self.assertTrue(area.contains(right), (area, right))
                self.assertLess(left.right(), right.left())


if __name__ == '__main__':
    unittest.main()
