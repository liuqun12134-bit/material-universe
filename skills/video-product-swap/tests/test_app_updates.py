import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import app_updates as updates
from app_version import REPOSITORY


class Response:
    def __init__(self, url, content, status=200):
        self.url, self.content, self.status_code = url, content, status
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def close(self): pass
    def raise_for_status(self):
        if self.status_code != 200: raise updates.requests.HTTPError("HTTP error")
    def iter_content(self, _): yield self.content


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.content = b"offline installer fixture"
        self.version = "1.11.0"
        self.item = {"name": "material-universe-setup-1.11.0.exe", "size": len(self.content), "sha256": hashlib.sha256(self.content).hexdigest()}
        self.manifest = {"format_version": 1, "repository": REPOSITORY, "version": self.version, "data_format": 1, "installer": self.item}
        self.url = updates.RELEASES_URL + "/download/v1.11.0/" + self.item["name"]
        self.update = {"version": self.version, "manifest": self.manifest, "download_url": self.url}
        self.release = {"tag_name": "v1.11.0", "draft": False, "prerelease": False, "assets": [
            {"name": "update.json", "browser_download_url": updates.RELEASES_URL + "/download/v1.11.0/update.json"},
            {**self.item, "browser_download_url": self.url}]}

    def session(self, release=None, manifest=None):
        release, manifest = release or self.release, manifest or self.manifest
        def get(url, **_):
            content = json.dumps(manifest if url.endswith("update.json") else release).encode()
            return Response(url, content)
        return Mock(get=Mock(side_effect=get))

    def test_check_only_uses_stable_version_matched_manifest(self):
        self.assertTrue(updates.check_update(session=self.session())["available"])
        self.manifest["version"] = "1.12.0"
        with self.assertRaisesRegex(ValueError, "版本"): updates.check_update(session=self.session())

    def test_numeric_versions_and_no_downgrade(self):
        self.assertGreater(updates.version_tuple("1.10.0"), updates.version_tuple("1.9.9"))
        self.assertFalse(updates.check_update(current="1.12.0", session=self.session())["available"])
        self.release["prerelease"] = True
        with self.assertRaises(ValueError): updates.check_update(session=self.session())

    def test_untrusted_urls_and_manifest_format_are_rejected(self):
        self.release["assets"][1]["browser_download_url"] = "https://evil.example/install.exe"
        with self.assertRaises(ValueError): updates.check_update(session=self.session())
        for name in ("../run.exe", "program.cmd", "other.exe"):
            self.item["name"] = name
            with self.assertRaises(ValueError): updates.validate_manifest(self.manifest)

    def test_download_checks_actual_bytes_and_reuses_valid_download(self):
        with tempfile.TemporaryDirectory() as folder:
            session = Mock(get=Mock(return_value=Response(self.url, self.content)))
            path = updates.download_update(self.update, folder, session=session)
            self.assertEqual(path.read_bytes(), self.content)
            updates.download_update(self.update, folder, session=session)
            session.get.assert_called_once()
            path.write_bytes(b"broken")
            bad = Mock(get=Mock(return_value=Response(self.url, b"corrupt")))
            with self.assertRaises(ValueError): updates.download_update(self.update, folder, session=bad)
            self.assertEqual(path.read_bytes(), b"broken")
            self.assertFalse(list(Path(folder).glob("*.part")))

    def test_newer_data_format_is_not_installed(self):
        self.manifest["data_format"] = 2
        with self.assertRaises(ValueError): updates.validate_manifest(self.manifest)

    def test_source_mode_does_not_spawn_installer(self):
        with patch.object(updates.subprocess, "Popen") as spawn:
            with self.assertRaises(ValueError): updates.start_installer(Path("anything.exe"), self.manifest, Path("unused"))
            spawn.assert_not_called()

    def test_missing_releases_is_not_a_fake_successful_update(self):
        session = Mock(get=Mock(return_value=Response(updates.API + "/releases/latest", b"{}", 404)))
        self.assertFalse(updates.check_update(session=session)["available"])

    def test_public_bundle_excludes_local_cases(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packaging"))
        from director_resources import stage_director, PUBLIC_EXAMPLES
        from personal_library import RULES
        with tempfile.TemporaryDirectory() as folder:
            output = stage_director(RULES, Path(folder) / "rules")
            actual = {p.parent.name for p in (output / "demo-library/entries").glob("*/record.json")}
            self.assertEqual(actual, set(PUBLIC_EXAMPLES))
            self.assertFalse(list(output.rglob("*.mp4")))


if __name__ == "__main__": unittest.main()
