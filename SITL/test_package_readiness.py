"""The packaged motor test must not raise throttle during startup/arming."""
import unittest
from unittest import mock

import windows_package_test as package_test


class PackageReadinessTests(unittest.TestCase):
    def test_waits_for_silence_after_arming(self):
        watch = mock.Mock(resolved=[1, 1])
        # Initial silence is stale when arming is first observed. Then the
        # arming tone is active; only its final falling edge means ready.
        watch.get.side_effect = [
            [(0., 0)], [(0., 0)],
            [(1., 1)], [(0., 0)],
            [(1., 1)], [(1., 1)],
            [(1., 1)], [(2., 0)],
        ]
        with mock.patch.object(package_test, 'WatchStream', return_value=watch), \
                mock.patch.object(package_test.time, 'sleep') as sleep:
            package_test._wait_for_firmware_ready(18471)
        self.assertEqual(sleep.call_count, 3)
        watch.close.assert_called_once()

    def test_missing_symbols_fail_explicitly(self):
        watch = mock.Mock(resolved=[1, 0])
        with mock.patch.object(package_test, 'WatchStream', return_value=watch):
            with self.assertRaisesRegex(RuntimeError, 'symbols unavailable'):
                package_test._wait_for_firmware_ready(18471)
        watch.close.assert_called_once()

    def test_unarmed_timeout_reports_last_state_and_closes_watch(self):
        watch = mock.Mock(resolved=[1, 1])
        watch.get.side_effect = [[(1., 0)], [(1., 0)]]
        with mock.patch.object(package_test, 'WatchStream', return_value=watch), \
                mock.patch.object(package_test.time, 'monotonic', side_effect=[0, 0, 2]), \
                mock.patch.object(package_test.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'throttle-ready: armed='):
                package_test._wait_for_firmware_ready(18471, timeout=1)
        watch.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
