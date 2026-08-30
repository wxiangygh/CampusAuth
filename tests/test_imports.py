"""导入完整性回归测试。

背景：应用是 PyInstaller 冻结的 GUI 程序，`from core.X import Y` 这类
"语法合法但名字不存在"的错误在构建阶段不会被发现（构建照样成功），
只有运行 exe 时才崩溃。典型事故：`from core.app_state import app_st`
（正确名字是 `app_state`）。

本测试用 AST 静态解析 tray_app.py 的所有 `from ... import ...`，
逐个校验被导入的名字在目标模块中确实存在，把这类故障挡在提交之前。
"""
import ast
import importlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ('tray_app.py', 'warp_exclusion.py', 'traffic_monitor.py')
CHECKED_MODULES = ('core', 'warp_exclusion', 'traffic_monitor')


def _iter_import_from(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield node


def _local_module_names():
    """返回仓库内可导入的本地模块名集合。"""
    names = {'warp_exclusion', 'traffic_monitor'}
    core_dir = ROOT / 'core'
    if core_dir.is_dir():
        for path in core_dir.glob('*.py'):
            if path.stem != '__init__':
                names.add(f'core.{path.stem}')
    return names


class ImportIntegrityTests(unittest.TestCase):
    def test_imported_names_exist(self):
        import sys

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        local_modules = _local_module_names()
        problems = []
        checked = 0

        for filename in TARGETS:
            source = ROOT / filename
            if not source.exists():
                continue
            tree = ast.parse(source.read_text(encoding='utf-8'), filename=filename)
            for node in _iter_import_from(tree):
                module = node.module or ''
                if module.split('.')[0] not in CHECKED_MODULES:
                    continue
                if module not in local_modules:
                    continue
                try:
                    imported = importlib.import_module(module)
                except Exception as exc:  # 模块自身导入失败也视为问题
                    problems.append(f'{filename}:{node.lineno} 无法导入 {module}: {exc}')
                    continue
                for alias in node.names:
                    if alias.name == '*':
                        continue
                    checked += 1
                    if not hasattr(imported, alias.name):
                        problems.append(
                            f'{filename}:{node.lineno} '
                            f'`from {module} import {alias.name}` 中 {alias.name} 不存在'
                        )

        self.assertGreater(checked, 0, '未校验到任何导入，检查测试逻辑')
        self.assertEqual([], problems, '\n'.join(problems))


if __name__ == '__main__':
    unittest.main()
