import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from core.config import ConfigStore, DEFAULT_AUTH_WORKFLOW


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

    def test_builtin_workflows_and_active_workflow_are_initialized(self):
        snapshot = self.store.snapshot()
        self.assertIn('default_auth', snapshot['workflows'])
        self.assertIn('portal_logout', snapshot['workflows'])
        self.assertIn('restart_warp', snapshot['workflows'])
        self.assertEqual(snapshot['active_workflow_id'], 'default_auth')
        self.assertEqual(snapshot['auth_workflow'], snapshot['workflows']['default_auth']['steps'])
        self.assertIn('portal_logout', [step['id'] for step in snapshot['workflows']['portal_reauth']['steps']])
        self.assertIn('restart_warp_service',
                      [step['id'] for step in snapshot['workflows']['restart_warp']['steps']])

    def test_legacy_single_workflow_is_migrated_to_custom_workflow(self):
        legacy_steps = [{'id': 'portal_login', 'enabled': True, 'timeout': 8}]
        self.path.write_text(json.dumps({'auth_workflow': legacy_steps}), encoding='utf-8')
        store = ConfigStore(self.path)
        snapshot = store.snapshot()
        self.assertEqual(snapshot['active_workflow_id'], 'legacy_auth')
        self.assertEqual(snapshot['workflows']['legacy_auth']['steps'], legacy_steps)
        self.assertEqual(snapshot['auth_workflow'], legacy_steps)

    def test_custom_workflow_can_be_saved_and_selected_independently(self):
        steps = [{'id': 'portal_logout', 'enabled': True, 'timeout': 6}]
        workflows = self.store.get('workflows')
        workflows['custom_logout'] = {
            'id': 'custom_logout', 'name': '只注销', 'built_in': False,
            'tray_menu': True, 'steps': steps,
        }
        saved = self.store.patch({'workflows': workflows, 'active_workflow_id': 'custom_logout'})
        self.assertEqual(saved['active_workflow_id'], 'custom_logout')
        self.assertEqual(saved['auth_workflow'], steps)
        reloaded = ConfigStore(self.path).snapshot()
        self.assertEqual(reloaded['workflows']['custom_logout']['steps'], steps)

    def test_default_granular_workflow_contains_independent_restart_node(self):
        ids = [step['id'] for step in DEFAULT_AUTH_WORKFLOW]
        self.assertIn('restart_warp_service',
                      [step['id'] for step in self.store.get('workflows')['restart_warp']['steps']])
        self.assertIn('portal_logout', self.store.get('workflows'))
        self.assertIn('set_warp_masque', ids)
        self.assertIn('reset_warp_masque', ids)

    @unittest.skipUnless(os.name == 'nt', 'DPAPI is Windows-only')
    def test_password_is_encrypted_at_rest_and_transparent_on_reload(self):
        self.store.patch({'password': 'secret-value'})
        stored = json.loads(self.path.read_text(encoding='utf-8'))['password']
        self.assertTrue(stored.startswith('dpapi:'))
        self.assertNotIn('secret-value', stored)
        self.assertEqual(ConfigStore(self.path).get('password'), 'secret-value')

    def test_workflow_rename_survives_patch_and_reload(self):
        """改名后保存：名称必须在 patch 与重新加载后都保留（回归：改名不生效）。"""
        workflows = self.store.get('workflows')
        workflows['default_auth'] = {**workflows['default_auth'], 'name': '我的认证流程'}
        self.store.patch({'workflows': workflows})
        self.assertEqual(self.store.get('workflows')['default_auth']['name'], '我的认证流程')
        reloaded = ConfigStore(self.path).snapshot()
        self.assertEqual(reloaded['workflows']['default_auth']['name'], '我的认证流程')

    def test_window_geometry_maximized_flag_roundtrip(self):
        """窗口几何需能保存 maximized 标记并在重启（reload）后保留。"""
        geometry = {'width': 1512, 'height': 962, 'x': 400, 'y': 216, 'maximized': True}
        self.store.patch({'window': geometry})
        saved = self.store.get('window')
        self.assertEqual(saved['width'], 1512)
        self.assertTrue(saved['maximized'])
        reloaded = ConfigStore(self.path).snapshot()
        self.assertTrue(reloaded['window']['maximized'])
        self.assertEqual(reloaded['window']['x'], 400)


if __name__ == '__main__':
    unittest.main()
