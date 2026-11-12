import pathlib
import subprocess


def test_popup_recognizes_only_official_social_domains():
    source = pathlib.Path(__file__).resolve().parents[1] / "src/web/account-report/popup.js"
    subprocess.run(
        [
            "node",
            "-e",
            r"""
      const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
      const ctx = { URL, fetch: () => new Promise(() => {}) };
      vm.createContext(ctx);
      vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
      for (const [url, expected] of [
        ['https://www.facebook.com/', 'facebook'], ['https://m.facebook.com/checkpoint', 'facebook'],
        ['https://instagram.com/', 'instagram'], ['https://www.tiktok.com/', 'tiktok'],
        ['https://twitter.com/', 'x'], ['https://x.com/', 'x'],
        ['https://mail.google.com/mail/u/0/', 'gmail'], ['https://accounts.google.com/', 'gmail'],
        ['https://outlook.live.com/', 'hotmail'], ['https://login.microsoftonline.com/', 'hotmail'],
        ['https://facebook.com.evil.test/', null], ['https://evilfacebook.com/', null],
        ['https://evil.test/facebook.com', null], ['https://facebook.com@evil.test/', null],
        ['https://user:pw@facebook.com/', null], ['http://facebook.com/', null],
        ['https://docs.google.com/', null], ['about:blank', null], ['javascript:alert(1)', null], ['???', null]
      ]) assert.equal(ctx.platformFor(url), expected, url);
    """,
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
