import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from core.config import ConfigStore


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / 'tray_config.json'
        self.store = ConfigStore(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_patch_preserves_unrelated_fields_and_returns_revision(self):
        first = self.store.patch({'username': 'alice'})
        second = self.store.patch({'wifi_name': 'Campus'})
        self.assertEqual(second['username'], 'alice')
        self.assertEqual(second['wifi_name'], 'Campus')
        self.assertGreater(second['_revision'], first['_revision'])
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['username'], 'alice')

    def test_concurrent_disjoint_patches_are_not_lost(self):
        barrier = threading.Barrier(3)

        def update(key, value):
            barrier.wait()
            self.store.patch({key: value})

        threads = [threading.Thread(target=update, args=('username', 'alice')),
                   threading.Thread(target=update, args=('wifi_name', 'Campus'))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['username'], 'alice')
        self.assertEqual(snapshot['wifi_name'], 'Campus')

    def test_stale_full_save_revision_is_rejected(self):
        revision = self.store.revision
        self.store.patch({'username': 'newer'})
        with self.assertRaisesRegex(ValueError, '其他操作'):
            self.store.patch({'username': 'stale'}, expected_revision=revision)
        self.assertEqual(self.store.get('username'), 'newer')


    @unittest.skipUnless(os.name == 'nt', 'DPAPI is Windows-only')
    def test_password_is_encrypted_at_rest_and_transparent_on_reload(self):
        self.store.patch({'password': 'secret-value'})
        stored = json.loads(self.path.read_text(encoding='utf-8'))['password']
        self.assertTrue(stored.startswith('dpapi:'))
        self.assertNotIn('secret-value', stored)
        self.assertEqual(ConfigStore(self.path).get('password'), 'secret-value')

if __name__ == '__main__':
    unittest.main()
