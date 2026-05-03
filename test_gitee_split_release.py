import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.updater import (
    UPDATE_SOURCE_GITEE,
    UpdateError,
    download_update,
    _manifest_to_update_info,
    _part_download_candidates,
    get_download_candidates,
)


class GiteeSplitReleaseTest(unittest.TestCase):
    def test_gitee_split_release_uses_part_urls_not_missing_full_zip(self):
        manifest = {
            "version": "9.9.9",
            "tag": "v9.9.9",
            "tag_name": "v9.9.9",
            "asset_name": "YHoAutoFish-v9.9.9-windows.zip",
            "download_url": "https://github.com/FADEDTUMI/YHoAutoFish/releases/latest/download/YHoAutoFish-v9.9.9-windows.zip",
            "download_urls": [
                "https://github.com/FADEDTUMI/YHoAutoFish/releases/latest/download/YHoAutoFish-v9.9.9-windows.zip",
            ],
            "gitee_release_tag": "9.9.9",
            "gitee_download_urls": [
                "https://gitee.com/fadedtumi/YHoAutoFish/releases/download/9.9.9/YHoAutoFish-v9.9.9-windows.zip",
            ],
            "gitee_asset_parts": [
                {
                    "name": "YHoAutoFish-v9.9.9-windows.zip.001",
                    "download_urls": [
                        "https://github.com/FADEDTUMI/YHoAutoFish/releases/download/v9.9.9/YHoAutoFish-v9.9.9-windows.zip.001",
                    ],
                    "gitee_download_urls": [
                        "https://gitee.com/fadedtumi/YHoAutoFish/releases/download/9.9.9/YHoAutoFish-v9.9.9-windows.zip.001",
                    ],
                },
                {
                    "name": "YHoAutoFish-v9.9.9-windows.zip.002",
                    "gitee_download_urls": [
                        "https://gitee.com/fadedtumi/YHoAutoFish/releases/download/9.9.9/YHoAutoFish-v9.9.9-windows.zip.002",
                    ],
                },
            ],
        }

        update_info = _manifest_to_update_info(manifest, current_version="0.0.0", source=UPDATE_SOURCE_GITEE)

        self.assertEqual((), get_download_candidates(update_info, source=UPDATE_SOURCE_GITEE))
        self.assertIn(
            "https://gitee.com/fadedtumi/YHoAutoFish/releases/download/9.9.9/YHoAutoFish-v9.9.9-windows.zip.001",
            _part_download_candidates(update_info, update_info.gitee_asset_parts[0], source=UPDATE_SOURCE_GITEE),
        )
        self.assertIn(
            "https://github.com/FADEDTUMI/YHoAutoFish/releases/download/v9.9.9/YHoAutoFish-v9.9.9-windows.zip.001",
            _part_download_candidates(update_info, update_info.gitee_asset_parts[0], source=UPDATE_SOURCE_GITEE),
        )

    def test_gitee_split_403_falls_back_to_full_package_candidates(self):
        manifest = {
            "version": "9.9.9",
            "tag": "v9.9.9",
            "tag_name": "v9.9.9",
            "asset_name": "YHoAutoFish-v9.9.9-windows.zip",
            "download_url": "https://github.com/FADEDTUMI/YHoAutoFish/releases/latest/download/YHoAutoFish-v9.9.9-windows.zip",
            "github_download_urls": [
                "https://github.com/FADEDTUMI/YHoAutoFish/releases/latest/download/YHoAutoFish-v9.9.9-windows.zip",
            ],
            "sha256": "0" * 64,
            "gitee_release_tag": "9.9.9",
            "gitee_asset_parts": [
                {
                    "name": "YHoAutoFish-v9.9.9-windows.zip.001",
                    "gitee_download_urls": [
                        "https://gitee.com/fadedtumi/YHoAutoFish/releases/download/9.9.9/YHoAutoFish-v9.9.9-windows.zip.001",
                    ],
                },
            ],
        }
        update_info = _manifest_to_update_info(manifest, current_version="0.0.0", source=UPDATE_SOURCE_GITEE)

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def fake_download_once(_url, target_path, **_kwargs):
                target_path.write_bytes(b"fallback package")

            with patch("core.updater._update_subdir", return_value=root), \
                patch("core.updater._cleanup_old_children"), \
                patch("core.updater._download_split_update", side_effect=UpdateError("HTTP Error 403: Forbidden")), \
                patch("core.updater._download_once", side_effect=fake_download_once) as download_once, \
                patch("core.updater._verify_sha256"):
                result = download_update(update_info, source=UPDATE_SOURCE_GITEE)

        self.assertTrue(result.endswith("YHoAutoFish-v9.9.9-windows.zip"))
        self.assertIn("github.com", download_once.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
