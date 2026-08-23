import unittest
from unittest.mock import patch

from core.warp_manager import probe_warp_status


class WarpStatusTests(unittest.TestCase):
    def test_connected_status(self):
        with patch('core.warp_manager.run_command', return_value=(0, 'Status update: Connected', '')):
            result = probe_warp_status('warp-cli')
        self.assertTrue(result.success)
        self.assertEqual(result.code, 'connected')

    def test_registration_error_is_actionable_and_not_retryable(self):
        with patch('core.warp_manager.run_command', return_value=(0, 'Registration Missing', '')):
            result = probe_warp_status('warp-cli')
        self.assertEqual(result.code, 'registration_required')
        self.assertFalse(result.retryable)
        self.assertIn('注册', result.message)

    def test_no_network_is_retryable(self):
        with patch('core.warp_manager.run_command', return_value=(0, 'Unable: No Network', '')):
            result = probe_warp_status('warp-cli')
        self.assertEqual(result.code, 'no_network')
        self.assertTrue(result.retryable)


if __name__ == '__main__':
    unittest.main()
