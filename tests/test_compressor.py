import os
from pathlib import Path
import subprocess
import tempfile
import unittest


BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "build.sh"
HELPERS = BUILD_SCRIPT.read_text().split("kconfig_symbol_supported() {", 1)[1]
HELPERS = "kconfig_symbol_supported() {" + HELPERS.split(
    "generate_modules_vhdx() (", 1
)[0]

CONFIG_SCRIPT = """#!/usr/bin/env python3
from pathlib import Path
import sys

config = Path(".config")
symbol = "CONFIG_" + sys.argv[2]
lines = [
    line for line in config.read_text().splitlines()
    if not line.startswith(symbol + "=") and line != "# " + symbol + " is not set"
]
lines.append(symbol + "=y" if sys.argv[1] == "--enable"
             else "# " + symbol + " is not set")
config.write_text("\\n".join(lines) + "\\n")
"""

OLDDEFCONFIG_SCRIPT = """#!/usr/bin/env python3
from pathlib import Path
import os

config = Path(".config")
lines = [
    line for line in config.read_text().splitlines()
    if not line.startswith("CONFIG_ZSWAP_COMPRESSOR_DEFAULT=")
]
for compressor in ("LZ4", "ZSTD"):
    choice = "CONFIG_ZSWAP_COMPRESSOR_DEFAULT_" + compressor + "=y"
    driver = "CONFIG_CRYPTO_" + compressor + "=y"
    if choice not in lines:
        continue
    if compressor in os.environ.get("REJECT_CHOICES", "").split(","):
        lines.remove(choice)
    else:
        lines.append('CONFIG_ZSWAP_COMPRESSOR_DEFAULT="' + compressor.lower() + '"')
    if compressor in os.environ.get("MODULAR_DRIVERS", "").split(","):
        lines = [line if line != driver else driver[:-1] + "m" for line in lines]
config.write_text("\\n".join(lines) + "\\n")
with Path("olddefconfig.calls").open("a") as calls:
    calls.write("olddefconfig\\n")
"""


class CompressorTests(unittest.TestCase):
    def configure(self, symbols=("LZ4", "ZSTD"), reject="", modular="", lz4="m"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "Kconfig").write_text(
                "".join(
                    f"config CRYPTO_{symbol}\n"
                    f"config ZSWAP_COMPRESSOR_DEFAULT_{symbol}\n"
                    for symbol in symbols
                )
            )
            # Simulate an existing zstd default without built-in LZ4 support.
            (root / ".config").write_text(
                "CONFIG_ZSWAP_COMPRESSOR_DEFAULT_ZSTD=y\n"
                'CONFIG_ZSWAP_COMPRESSOR_DEFAULT="zstd"\n'
                f"CONFIG_CRYPTO_LZ4={lz4}\n"
            )
            for name, content in (
                ("config", CONFIG_SCRIPT),
                ("olddefconfig", OLDDEFCONFIG_SCRIPT),
            ):
                script = root / "scripts" / name
                script.write_text(content)
                script.chmod(0o755)

            result = subprocess.run(
                [
                    "bash", "-c",
                    "set -e -o pipefail\n" + HELPERS
                    + '\nmake() { test "$*" = olddefconfig && ./scripts/olddefconfig; }\n'
                    + "configure_zswap_compressor\n",
                ],
                cwd=root,
                env={**os.environ, "REJECT_CHOICES": reject,
                     "MODULAR_DRIVERS": modular},
                text=True,
                capture_output=True,
            )
            config = (root / ".config").read_text().splitlines()
            calls = root / "olddefconfig.calls"
            count = len(calls.read_text().splitlines()) if calls.exists() else 0
            return result, config, count

    def test_prefers_lz4_and_builds_in_modular_driver(self):
        result, config, count = self.configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CONFIG_CRYPTO_LZ4=y", config)
        self.assertIn('CONFIG_ZSWAP_COMPRESSOR_DEFAULT="lz4"', config)
        self.assertNotIn("CONFIG_ZSWAP_COMPRESSOR_DEFAULT_ZSTD=y", config)
        self.assertEqual(count, 1)

    def test_uses_lz4_without_zstd_support(self):
        result, config, _ = self.configure(symbols=("LZ4",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CONFIG_ZSWAP_COMPRESSOR_DEFAULT="lz4"', config)

    def test_builds_in_previously_disabled_lz4_driver(self):
        result, config, _ = self.configure(lz4="n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CONFIG_CRYPTO_LZ4=y", config)
        self.assertIn('CONFIG_ZSWAP_COMPRESSOR_DEFAULT="lz4"', config)

    def test_falls_back_when_lz4_is_absent(self):
        result, config, count = self.configure(symbols=("ZSTD",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CONFIG_CRYPTO_ZSTD=y", config)
        self.assertIn('CONFIG_ZSWAP_COMPRESSOR_DEFAULT="zstd"', config)
        self.assertEqual(count, 1)

    def test_falls_back_when_kconfig_rejects_lz4(self):
        result, config, count = self.configure(reject="LZ4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CONFIG_ZSWAP_COMPRESSOR_DEFAULT="zstd"', config)
        self.assertIn("Warning:", result.stderr)
        self.assertEqual(count, 2)

    def test_falls_back_when_lz4_driver_is_not_built_in(self):
        result, config, count = self.configure(modular="LZ4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CONFIG_ZSWAP_COMPRESSOR_DEFAULT="zstd"', config)
        self.assertNotIn("CONFIG_ZSWAP_COMPRESSOR_DEFAULT_LZ4=y", config)
        self.assertEqual(count, 2)

    def test_fails_when_neither_choice_is_available(self):
        result, _, count = self.configure(symbols=())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error: Neither LZ4 nor zstd", result.stderr)
        self.assertEqual(count, 0)

    def test_fails_when_kconfig_rejects_both(self):
        result, _, count = self.configure(reject="LZ4,ZSTD")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error: Neither LZ4 nor zstd", result.stderr)
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
