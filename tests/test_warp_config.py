import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.state
from core.warp_manager import _set_warp_endpoint_ipv6


class WarpConfigTests(unittest.TestCase):
    def test_endpoint_override_has_persistent_backup_and_restores_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            cloudflare = Path(directory) / 'Cloudflare'
            cloudflare.mkdir()
            config_path = cloudflare / 'conf.json'
            original = {'endpoints': [{'v4': '162.159.1.1:443', 'v6': '[2606::1]:443'}],
                        'unrelated': {'keep': True}}
            config_path.write_text(json.dumps(original), encoding='utf-8')
            core.state._conf_json_backup = None
            with patch.dict(os.environ, {'ProgramData': directory}):
                self.assertTrue(_set_warp_endpoint_ipv6(True))
                modified = json.loads(config_path.read_text(encoding='utf-8'))
                self.assertEqual(modified['endpoints'][0]['v4'], '')
                self.assertTrue((cloudflare / 'conf.json.campusauth.bak').exists())
                # Simulate a restart: restoration must work without memory state.
                core.state._conf_json_backup = None
                self.assertTrue(_set_warp_endpoint_ipv6(False))
            self.assertEqual(json.loads(config_path.read_text(encoding='utf-8')), original)
            self.assertFalse((cloudflare / 'conf.json.campusauth.bak').exists())


if __name__ == '__main__':
    unittest.main()
