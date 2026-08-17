import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from core.management.commands.compilemessages import _update_module_messages


class UpdateModuleMessagesTest(TestCase):
    def setUp(self):
        self.original_directory = os.getcwd()

    def tearDown(self):
        os.chdir(self.original_directory)

    def test_runs_makemessages_inside_each_module_and_restores_directory(self):
        with TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            module_paths = {
                "core": root / "core" / "__init__.py",
                "location": root / "location" / "__init__.py",
            }
            for path in module_paths.values():
                path.parent.mkdir()
                path.touch()

            @contextmanager
            def module_path(module_name, _resource_name):
                yield module_paths[module_name]

            visited_directories = []

            def record_directory(*_args, **_kwargs):
                visited_directories.append(Path.cwd())

            os.chdir(root)
            with mock.patch(
                "core.management.commands.compilemessages.resources.path",
                side_effect=module_path,
            ), mock.patch(
                "core.management.commands.compilemessages.call_command",
                side_effect=record_directory,
            ) as mocked_call:
                directories = _update_module_messages(
                    [{"name": "core"}, {"name": "location"}], ["en"]
                )

            self.assertEqual(
                visited_directories,
                [module_paths["core"].parent, module_paths["location"].parent],
            )
            self.assertEqual(directories, visited_directories)
            self.assertEqual(Path.cwd(), root)
            self.assertEqual(mocked_call.call_count, 2)

    def test_restores_directory_when_makemessages_fails(self):
        with TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            module_path = root / "core" / "__init__.py"
            module_path.parent.mkdir()
            module_path.touch()

            @contextmanager
            def resolved_path(_module_name, _resource_name):
                yield module_path

            os.chdir(root)
            with mock.patch(
                "core.management.commands.compilemessages.resources.path",
                side_effect=resolved_path,
            ), mock.patch(
                "core.management.commands.compilemessages.call_command",
                side_effect=RuntimeError("message extraction failed"),
            ):
                with self.assertRaises(RuntimeError):
                    _update_module_messages([{"name": "core"}], ["en"])

            self.assertEqual(Path.cwd(), root)
