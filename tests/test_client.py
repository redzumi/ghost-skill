import argparse
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ghost_client", ROOT / "scripts/ghost.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
KEY = "a" * 24 + ":" + "b" * 64
POST_ID = "c" * 24


class FakeGhost:
    def __init__(self, status="draft"):
        self.url = "https://blog.example.invalid"
        self.current = {"id": POST_ID, "title": "Fixture", "slug": "fixture", "status": status, "updated_at": "2026-01-01T00:00:00.000Z", "tags": []}
        self.calls = []

    def editor_url(self, post_id):
        return self.url + "/ghost/#/editor/post/" + post_id

    def request(self, method, path, params=None, body=None, **kwargs):
        self.calls.append((method, path, params, body, kwargs))
        if path == "images/upload/":
            return {"images": [{"url": self.url + "/content/images/fixture.png"}]}
        post = dict(self.current)
        if body:
            post.update(body["posts"][0])
        return {"posts": [post]}


def args(**overrides):
    values = dict(md=None, html=None, title=None, slug=None, excerpt=None, feature_image=None, og_image=None, twitter_image=None, tag=None, dry_run=False, ref=POST_ID, md_title=False, allow_published=False, execute=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = mock.patch.object(client, "CREDENTIALS", str(self.root / "credentials.env"))
        config.start()
        self.addCleanup(config.stop)
        env = mock.patch.dict(client.os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def output(self, fn, ghost, arguments):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            fn(ghost, arguments)
        return json.loads(stdout.getvalue())

    def test_markdown_title_frontmatter_and_private_tail(self):
        source = self.root / "article.md"
        source.write_text('---\ntitle: Frontmatter title\n---\n# Heading title\n\nPublic body\n<!-- ghost:end -->\nPrivate local notes')
        title, meta, body = client.read_markdown(source)
        self.assertEqual(title, "Heading title")
        self.assertEqual(meta["title"], "Frontmatter title")
        self.assertEqual(body, "Public body\n")

    def test_markdown_renderer_uses_local_stdin(self):
        result = mock.Mock(stdout="<p>Fixture</p>")
        with mock.patch.object(client.subprocess, "run", return_value=result) as run:
            self.assertEqual(client.markdown_to_html("Fixture"), "<p>Fixture</p>")
        self.assertEqual(run.call_args.kwargs["input"], "Fixture")
        self.assertIn("--package=marked@18.1.2", run.call_args.args[0])

    def test_create_always_drafts(self):
        ghost = FakeGhost()
        with mock.patch.object(client, "load_content", return_value=("Fixture", "<p>Body</p>", {"status": "published"})):
            self.output(client.cmd_create, ghost, args())
        self.assertEqual(len(ghost.calls), 1)
        self.assertEqual(ghost.calls[0][3]["posts"][0]["status"], "draft")
        self.assertEqual(ghost.calls[0][2], {"source": "html"})

    def test_create_preview_does_not_send(self):
        ghost = FakeGhost()
        with mock.patch.object(client, "load_content", return_value=("Fixture", "<p>Body</p>", {})):
            result = self.output(client.cmd_create, ghost, args(dry_run=True))
        self.assertTrue(result["dry_run"])
        self.assertEqual(ghost.calls, [])

    def test_update_preserves_title_and_uses_current_timestamp(self):
        ghost = FakeGhost()
        with mock.patch.object(client, "load_content", return_value=("New H1", "<p>Body</p>", {})):
            self.output(client.cmd_update, ghost, args())
        write = ghost.calls[-1]
        self.assertEqual(write[0], "PUT")
        self.assertEqual(write[3]["posts"][0]["updated_at"], ghost.current["updated_at"])
        self.assertNotIn("title", write[3]["posts"][0])

    def test_published_update_requires_flag(self):
        ghost = FakeGhost("published")
        with mock.patch.object(client, "load_content", return_value=(None, "<p>Body</p>", {})), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                client.cmd_update(ghost, args())
        self.assertEqual([c[0] for c in ghost.calls], ["GET"])

    def test_publish_preview_never_writes(self):
        ghost = FakeGhost()
        result = self.output(client.cmd_publish, ghost, args())
        self.assertTrue(result["dry_run"])
        self.assertEqual([c[0] for c in ghost.calls], ["GET"])

    def test_explicit_publish_writes_once(self):
        ghost = FakeGhost()
        self.output(client.cmd_publish, ghost, args(execute=True))
        writes = [c for c in ghost.calls if c[0] == "PUT"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][3]["posts"][0], {"status": "published", "updated_at": ghost.current["updated_at"]})

    def test_image_upload_and_filename_rejection(self):
        ghost = FakeGhost()
        image = self.root / "cover.png"
        image.write_bytes(b"synthetic image")
        self.output(client.cmd_upload_image, ghost, argparse.Namespace(file=str(image)))
        self.assertIn(b"synthetic image", ghost.calls[0][4]["raw"])
        self.assertIn('filename="cover.png"', ghost.calls[0][4]["raw"].decode())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            client.cmd_upload_image(FakeGhost(), argparse.Namespace(file="bad\nname.png"))

    def test_url_validation(self):
        for url in ["http://blog.example.invalid", "https://user:pass@blog.example.invalid", "https://blog.example.invalid/?secret=x", "https://blog.example.invalid/#fragment", "https://blog.example.invalid/../other"]:
            with self.subTest(url=url), mock.patch.dict(client.os.environ, {"GHOST_API_URL": url, "GHOST_ADMIN_API_KEY": KEY}), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                client.load_config()
        with mock.patch.dict(client.os.environ, {"GHOST_API_URL": "https://blog.example.invalid", "GHOST_ADMIN_API_KEY": KEY}):
            self.assertEqual(client.load_config(), ("https://blog.example.invalid", KEY))

    def test_config_permissions(self):
        path = Path(client.CREDENTIALS)
        path.write_text("GHOST_API_URL=https://blog.example.invalid\nGHOST_ADMIN_API_KEY=" + KEY)
        path.chmod(0o644)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            client.load_config()
        path.chmod(0o600)
        self.assertEqual(client.load_config()[1], KEY)

    def test_jwt_is_short_lived(self):
        with mock.patch.object(client.time, "time", return_value=1000):
            token = client.make_token(KEY)
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        self.assertEqual(data, {"iat": 1000, "exp": 1300, "aud": "/admin/"})

    def test_redirect_refusal(self):
        request = urllib.request.Request("https://blog.example.invalid/ghost/api/admin/posts/")
        with self.assertRaises(urllib.error.HTTPError) as error:
            client.NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://evil.invalid/")
        error.exception.close()

    def test_http_errors_do_not_dump_server_body(self):
        ghost = object.__new__(client.Ghost)
        ghost.url, ghost.key = "https://blog.example.invalid", KEY
        error = urllib.error.HTTPError(ghost.url, 403, "denied", {}, io.BytesIO(("private body " + KEY).encode()))
        stderr = io.StringIO()
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch.object(client.urllib.request, "build_opener", return_value=opener), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            ghost.request("GET", "posts/")
        self.assertIn("HTTP 403", stderr.getvalue())
        self.assertNotIn(KEY, stderr.getvalue())
        self.assertNotIn("private body", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
