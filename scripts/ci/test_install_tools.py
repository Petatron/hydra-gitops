"""The real installer must reject a release asset that differs from the reviewed hash."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InstallerTests(unittest.TestCase):
    def test_tampered_download_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            curl = fake_bin / "curl"
            curl.write_text("""#!/bin/sh
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-o' ]; then
    printf 'tampered release asset' > "$2"
    exit 0
  fi
  shift
done
exit 1
""")
            curl.chmod(0o755)
            destination = root / "installed"
            env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
            result = subprocess.run(["bash", str(Path(__file__).with_name("install-tools.sh")), str(destination)],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FAILED", result.stdout + result.stderr)
            self.assertEqual(list(destination.iterdir()), [])
