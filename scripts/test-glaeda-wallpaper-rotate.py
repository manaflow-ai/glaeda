#!/usr/bin/env python3
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/glaeda-wallpaper-rotate"


class WallpaperRotateTests(unittest.TestCase):
    def test_rotates_image_choices_and_advances_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            store = home / "Library/Application Support/com.apple.wallpaper/Store"
            store.mkdir(parents=True)
            images = root / "wallpapers"
            images.mkdir()
            (images / "02-second.jpg").write_bytes(b"second")
            (images / "01-first.jpg").write_bytes(b"first")
            document = {
                "AllSpacesAndDisplays": {
                    "Linked": {"Content": {"Choices": [{"Provider": "aerial"}]}},
                    "Idle": {"Content": {"Choices": [{"Provider": "screen-saver"}]}}},
                "SystemDefault": {
                    "Desktop": {"Content": {"Choices": [{"Provider": "image"}], "EncodedOptionValues": "$null"}},
                    "Idle": {"Content": {"Choices": [{"Provider": "screen-saver"}]}}},
                "Displays": {
                    "display-1": {
                        "Desktop": {
                            "Content": {
                                "Choices": [{"Provider": "image", "Configuration": b"old"}],
                                "EncodedOptionValues": b"old",
                            }},
                        "Idle": {"Content": {"Choices": [{"Provider": "screen-saver"}]}}}},
                "Spaces": {
                    "space-1": {
                        "Default": {
                            "Desktop": {"Content": {"Choices": [{"Provider": "image"}]}},
                            "Idle": {"Content": {"Choices": [{"Provider": "screen-saver"}]}}}}},
            }
            plist = store / "Index.plist"
            plist.write_bytes(plistlib.dumps(document, fmt=plistlib.FMT_BINARY))
            state = home / "state/wallpaper.json"
            env = {
                **os.environ,
                "HOME": str(home),
                "GLAEDA_WALLPAPER_DIR": str(images),
                "GLAEDA_WALLPAPER_STATE": str(state),
            }
            first = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=True)
            self.assertIn("01-first.jpg (4 spaces)", first.stdout)
            updated = plistlib.loads(plist.read_bytes())
            desktop_nodes = [
                updated["AllSpacesAndDisplays"]["Desktop"],
                updated["SystemDefault"]["Desktop"],
                updated["Displays"]["display-1"]["Desktop"],
                updated["Spaces"]["space-1"]["Default"]["Desktop"],
            ]
            for desktop in desktop_nodes:
                content = desktop["Content"]
                choice = content["Choices"][0]
                self.assertEqual(choice["Provider"], "com.apple.wallpaper.choice.image")
                self.assertEqual(choice["Files"], [])
                config = plistlib.loads(choice["Configuration"])
                self.assertEqual(config["type"], "imageFile")
                self.assertTrue(config["url"]["relative"].endswith("01-first.jpg"))
                self.assertEqual(plistlib.loads(content["EncodedOptionValues"]), {"values": {}})
            self.assertEqual(
                updated["AllSpacesAndDisplays"]["Idle"]["Content"]["Choices"][0]["Provider"], "screen-saver")
            self.assertEqual(updated["SystemDefault"]["Idle"]["Content"]["Choices"][0]["Provider"], "screen-saver")
            self.assertEqual(
                updated["Displays"]["display-1"]["Idle"]["Content"]["Choices"][0]["Provider"], "screen-saver")
            self.assertEqual(
                updated["Spaces"]["space-1"]["Default"]["Idle"]["Content"]["Choices"][0]["Provider"], "screen-saver")
            self.assertTrue((state.parent / "Index.plist.previous").is_file())
            self.assertEqual(json.loads(state.read_text())["index"], 1)

            second = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=True)
            self.assertIn("02-second.jpg", second.stdout)
            self.assertEqual(json.loads(state.read_text())["index"], 2)

    def test_missing_store_and_images_are_successful_noops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "HOME": str(root), "GLAEDA_WALLPAPER_DIR": str(root / "none")}
            result = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=True)
            self.assertIn("no GUI wallpaper store", result.stdout)


if __name__ == "__main__":
    unittest.main()
