#!/usr/bin/env python3
"""Ghost Admin API CLI for your configured site: list/read posts, create and update drafts, upload images.

Credentials: ~/.config/ghost/credentials.env (0600) with GHOST_API_URL and GHOST_ADMIN_API_KEY (id:secret).
Environment variables override the file. The key is never printed or passed in argv.
"""
import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CREDENTIALS = os.path.expanduser('~/.config/ghost/credentials.env')
ACCEPT_VERSION = 'v6.0'
MARKED = 'marked@18.1.2'
END_MARKER = '<!-- ghost:end -->'


def die(msg, code=1):
    print(f'error: {msg}', file=sys.stderr)
    sys.exit(code)


def load_config():
    cfg = {}
    if os.path.exists(CREDENTIALS):
        if os.stat(CREDENTIALS).st_mode & 0o077:
            die(f'{CREDENTIALS} must be mode 0600')
        for line in Path(CREDENTIALS).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip()
    for k in ('GHOST_API_URL', 'GHOST_ADMIN_API_KEY'):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if not cfg.get('GHOST_API_URL') or not cfg.get('GHOST_ADMIN_API_KEY'):
        die(f'GHOST_API_URL and GHOST_ADMIN_API_KEY are required ({CREDENTIALS})')
    if not re.fullmatch(r'[0-9a-fA-F]{24}:[0-9a-fA-F]{64}', cfg['GHOST_ADMIN_API_KEY']):
        die('GHOST_ADMIN_API_KEY must look like <id>:<hex secret>')
    base = cfg['GHOST_API_URL'].rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(p in ('.', '..') for p in urllib.parse.unquote(parsed.path).split('/'))):
        die('GHOST_API_URL must be an HTTPS site URL without credentials, query or fragment')
    return base, cfg['GHOST_ADMIN_API_KEY']


def make_token(key):
    kid, secret = key.split(':')
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=')
    iat = int(time.time())
    header = b64(json.dumps({'alg': 'HS256', 'typ': 'JWT', 'kid': kid}).encode())
    payload = b64(json.dumps({'iat': iat, 'exp': iat + 300, 'aud': '/admin/'}).encode())
    sig = b64(hmac.new(bytes.fromhex(secret), header + b'.' + payload, hashlib.sha256).digest())
    return (header + b'.' + payload + b'.' + sig).decode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Redirect refused', headers, fp)


class Ghost:
    def __init__(self):
        self.url, self.key = load_config()

    def request(self, method, path, params=None, body=None, raw=None, content_type=None):
        url = f'{self.url}/ghost/api/admin/{path.lstrip("/")}'
        if params:
            url += '?' + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {'Authorization': f'Ghost {make_token(self.key)}', 'Accept-Version': ACCEPT_VERSION}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        elif content_type:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            e.close()
            die(f'HTTP {e.code} {method} {path}')
        except urllib.error.URLError as e:
            die(f'{method} {path}: {e.reason}')

    def editor_url(self, post_id):
        return f'{self.url}/ghost/#/editor/post/{post_id}'


def read_markdown(path):
    text = Path(path).read_text(encoding='utf-8')
    meta = {}
    m = re.match(r'^---\n(.*?)\n---\n', text, re.S)
    if m:
        text = text[m.end():]
        for line in m.group(1).splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                meta[k.strip()] = v.strip().strip('"\'')
    if END_MARKER in text:
        text = text.split(END_MARKER, 1)[0]
    title = None
    h1 = re.search(r'^# (.+)$', text, re.M)
    if h1 and not text[:h1.start()].strip():
        title = h1.group(1).strip()
        text = text[h1.end():]
    return title, meta, text.strip() + '\n'


def markdown_to_html(md):
    try:
        out = subprocess.run(
            ['npx', '--yes', f'--package={MARKED}', '--', 'marked', '--gfm'],
            input=md, capture_output=True, text=True, timeout=180, check=True,
        )
    except FileNotFoundError:
        die('npx not found: Node.js is required to convert Markdown')
    except subprocess.CalledProcessError as e:
        die(f'marked failed: {e.stderr[:500]}')
    return out.stdout


def load_content(args):
    """Returns (title, html, meta) from --md or --html."""
    if args.md:
        title, meta, md = read_markdown(args.md)
        return title, markdown_to_html(md), meta
    if args.html:
        return None, Path(args.html).read_text(encoding='utf-8'), {}
    return None, None, {}


def summarize(p, ghost):
    return {
        'id': p['id'],
        'title': p.get('title'),
        'slug': p.get('slug'),
        'status': p.get('status'),
        'updated_at': p.get('updated_at'),
        'published_at': p.get('published_at'),
        'tags': [t['name'] for t in p.get('tags') or []],
        'url': p.get('url'),
        'editor': ghost.editor_url(p['id']),
    }


def find_post(ghost, ref, formats=None):
    params = {'formats': formats, 'include': 'tags'}
    if re.fullmatch(r'[0-9a-f]{24}', ref):
        return ghost.request('GET', f'posts/{ref}/', params)['posts'][0]
    return ghost.request('GET', f'posts/slug/{urllib.parse.quote(ref, safe="")}/', params)['posts'][0]


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_check(ghost, args):
    site = ghost.request('GET', 'site/')['site']
    ghost.request('GET', 'posts/', {'limit': 1, 'fields': 'id'})
    out({'site': site.get('title'), 'url': site.get('url'), 'version': site.get('version'), 'auth': 'ok'})


def cmd_posts(ghost, args):
    params = {'limit': args.limit, 'order': 'updated_at desc', 'include': 'tags'}
    if args.status != 'all':
        params['filter'] = f'status:{args.status}'
    out([summarize(p, ghost) for p in ghost.request('GET', 'posts/', params)['posts']])


def cmd_get(ghost, args):
    p = find_post(ghost, args.ref, formats='html' if args.html else None)
    res = summarize(p, ghost)
    res['custom_excerpt'] = p.get('custom_excerpt')
    res['feature_image'] = p.get('feature_image')
    res['og_image'] = p.get('og_image')
    res['twitter_image'] = p.get('twitter_image')
    if args.html:
        res['html'] = p.get('html')
    out(res)


def build_fields(args, meta):
    fields = {}
    title = args.title or meta.get('title')
    if title:
        fields['title'] = title
    for attr in ('slug', 'excerpt', 'feature_image', 'og_image', 'twitter_image'):
        val = getattr(args, attr, None)
        if val is not None:
            fields['custom_excerpt' if attr == 'excerpt' else attr] = val
    if args.tag:
        fields['tags'] = [{'name': t} for t in args.tag]
    return fields


def cmd_create(ghost, args):
    title, html, meta = load_content(args)
    if html is None:
        die('pass --md or --html')
    post = build_fields(args, meta)
    post.setdefault('title', title)
    if not post.get('title'):
        die('no title: pass --title or start the Markdown with "# Title"')
    post['html'] = html
    post['status'] = 'draft'
    if args.dry_run:
        out({'dry_run': True, **{k: v for k, v in post.items() if k != 'html'}, 'html_chars': len(html)})
        return
    p = ghost.request('POST', 'posts/', {'source': 'html'}, {'posts': [post]})['posts'][0]
    out(summarize(p, ghost))


def cmd_update(ghost, args):
    current = find_post(ghost, args.ref)
    title, html, meta = load_content(args)
    post = build_fields(args, meta)
    if title and 'title' not in post and args.md_title:
        post['title'] = title
    if html is not None:
        post['html'] = html
    if not post:
        die('nothing to update')
    if current['status'] != 'draft' and not args.allow_published:
        die(f'post is {current["status"]}; pass --allow-published to edit it')
    post['updated_at'] = current['updated_at']
    if args.dry_run:
        out({'dry_run': True, 'id': current['id'], 'status': current['status'],
             **{k: v for k, v in post.items() if k != 'html'}, 'html_chars': len(post.get('html') or '')})
        return
    params = {'source': 'html'} if html is not None else None
    p = ghost.request('PUT', f'posts/{current["id"]}/', params, {'posts': [post]})['posts'][0]
    out(summarize(p, ghost))


def cmd_publish(ghost, args):
    current = find_post(ghost, args.ref)
    if current['status'] == 'published':
        die('already published')
    if not args.execute:
        out({'dry_run': True, 'would_publish': summarize(current, ghost),
             'hint': 'add --execute only after the user explicitly confirmed publishing'})
        return
    body = {'posts': [{'status': 'published', 'updated_at': current['updated_at']}]}
    p = ghost.request('PUT', f'posts/{current["id"]}/', None, body)['posts'][0]
    out(summarize(p, ghost))


def cmd_upload_image(ghost, args):
    path = args.file
    ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    if not ctype.startswith('image/'):
        die(f'not an image: {ctype}')
    boundary = uuid.uuid4().hex
    name = os.path.basename(path)
    if any(c in name for c in '\r\n\"'):
        die('image filename must not contain quotes or line breaks')
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f'Content-Type: {ctype}\r\n\r\n'
    ).encode() + Path(path).read_bytes() + (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nimage'
        f'\r\n--{boundary}--\r\n'
    ).encode()
    res = ghost.request('POST', 'images/upload/', raw=body,
                        content_type=f'multipart/form-data; boundary={boundary}')
    out(res['images'][0])


def main(argv=None):
    ap = argparse.ArgumentParser(prog='ghost', description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('check', help='verify credentials and show site info')

    p = sub.add_parser('posts', help='list posts')
    p.add_argument('--status', choices=['all', 'draft', 'published', 'scheduled'], default='all')
    p.add_argument('--limit', type=int, default=15)

    p = sub.add_parser('get', help='show a post by id or slug')
    p.add_argument('ref')
    p.add_argument('--html', action='store_true', help='include rendered HTML')

    def content_args(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument('--md', help='Markdown file (frontmatter stripped, leading "# Title" used as title)')
        g.add_argument('--html', help='HTML file')
        p.add_argument('--title')
        p.add_argument('--slug')
        p.add_argument('--excerpt', help='custom excerpt')
        p.add_argument('--feature-image', dest='feature_image', help='image URL (see upload-image)')
        p.add_argument('--og-image', dest='og_image', help='social preview image URL (Facebook/LinkedIn/Telegram)')
        p.add_argument('--twitter-image', dest='twitter_image', help='X/Twitter card image URL')
        p.add_argument('--tag', action='append', help='tag name, repeatable; replaces existing tags')
        p.add_argument('--dry-run', action='store_true')

    p = sub.add_parser('create', help='create a DRAFT post')
    content_args(p)

    p = sub.add_parser('update', help='update a post (drafts only unless --allow-published)')
    p.add_argument('ref')
    content_args(p)
    p.add_argument('--md-title', action='store_true', help='also replace the title from the Markdown H1')
    p.add_argument('--allow-published', action='store_true')

    p = sub.add_parser('publish', help='publish a draft (dry run unless --execute)')
    p.add_argument('ref')
    p.add_argument('--execute', action='store_true')

    p = sub.add_parser('upload-image', help='upload an image, prints its URL')
    p.add_argument('file')

    args = ap.parse_args(argv)
    ghost = Ghost()
    {
        'check': cmd_check, 'posts': cmd_posts, 'get': cmd_get, 'create': cmd_create,
        'update': cmd_update, 'publish': cmd_publish, 'upload-image': cmd_upload_image,
    }[args.cmd](ghost, args)


if __name__ == '__main__':
    main()
