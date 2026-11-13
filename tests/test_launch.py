import unittest
from pathlib import Path
from unittest.mock import patch

from interfaces import launch


class LaunchTest(unittest.TestCase):
    def test_passes_arguments_to_tui_with_the_runtime_interpreter(self):
        bundle = Path(__file__)
        with (
            patch.object(launch, "ui_bundle_path", return_value=bundle),
            patch.object(launch, "initialize_config_directory"),
            patch.object(launch.shutil, "which", return_value="node"),
            patch.object(launch.subprocess, "call", return_value=0) as call,
            patch.object(launch.sys, "argv", ["RADE", "--help"]),
            patch.dict(launch.os.environ),
            self.assertRaises(SystemExit) as exit,
        ):
            launch.main()
        self.assertEqual(exit.exception.code, 0)
        call.assert_called_once_with(["node", str(bundle), "--help"])
