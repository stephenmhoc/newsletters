from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from newsletter_summary.config import load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


class ConfigTests(unittest.TestCase):
    def test_example_config_loads_with_the_new_sections(self) -> None:
        config = load_config(EXAMPLE)
        self.assertEqual(config.rss.all_feed_max_items, 50)
        self.assertEqual(config.rss.publication_names, {})
        self.assertTrue(config.images.proxy)
        self.assertEqual(config.images.cache_dir, EXAMPLE.parent / "var/image-cache")
        self.assertEqual((config.images.cache_max_mb, config.images.max_image_mb), (512, 10))

    def test_publication_names_are_keyed_by_lowercase_address_and_limits_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "gmail: {label: L}\n"
                "rss: {publication_names: {'Hi@Puck.Example': ' The Backstory ', 'blank@example.com': ''}}\n"
                "images: {proxy: false, cache_dir: /tmp/images}\n"
            )
            config = load_config(path)
            self.assertEqual(config.rss.publication_names, {"hi@puck.example": "The Backstory"})
            self.assertFalse(config.images.proxy)
            self.assertEqual(config.images.cache_dir, Path("/tmp/images"))
            for bad in ("rss: {all_feed_max_items: 0}", "images: {cache_max_mb: 0}"):
                path.write_text(f"gmail: {{label: L}}\n{bad}\n")
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
