# ghost-skill

Manage Ghost posts from a local Python CLI or a coding agent. Create drafts from
Markdown, edit metadata, upload images and publish when ready.

Requires Python 3.11+. Markdown conversion also needs Node.js 20+ and npx, which
runs the pinned `marked@18.1.2` package. No Python dependencies or MCP server are
required. The client uses the Ghost v6 Admin API header.

## Configure

Create a custom integration in Ghost Admin and store its API key privately.
Set these environment variables, or put them in `~/.config/ghost/credentials.env`
with mode 0600:

```text
GHOST_API_URL=https://blog.example.com
GHOST_ADMIN_API_KEY=<id>:<hex-secret>
```

The key has the permissions of the Ghost integration. Never commit it or paste
it into agent prompts. Requests use HTTPS, sign short-lived JWTs and refuse
redirects. Error messages omit server response bodies.

## Write and review

```sh
python3 scripts/ghost.py check
python3 scripts/ghost.py posts --status draft
python3 scripts/ghost.py create --md article.md --dry-run
python3 scripts/ghost.py create --md article.md
python3 scripts/ghost.py get POST_ID --html
python3 scripts/ghost.py update POST_ID --md article.md --dry-run
python3 scripts/ghost.py update POST_ID --md article.md
```

New posts are always drafts. Updates include the current `updated_at` value, so
concurrent edits can fail rather than overwrite each other. Editing a published
post requires `--allow-published`.

## Images and publication

```sh
python3 scripts/ghost.py upload-image cover.png
python3 scripts/ghost.py update POST_ID --feature-image IMAGE_URL --og-image IMAGE_URL
python3 scripts/ghost.py publish POST_ID
python3 scripts/ghost.py publish POST_ID --execute
```

The first publish command is a preview. Only `--execute` publishes.
The CLI has no delete command and does not retry writes automatically.

## Markdown and metadata

A leading H1 supplies the post title. Simple frontmatter is stripped; it can
provide a title. Text after `<!-- ghost:end -->` is left out of the post.
Updating from Markdown preserves the existing title unless `--md-title` is set.

Use `--slug`, `--excerpt`, `--feature-image`, `--og-image`, `--twitter-image`, or
repeat `--tag`. Tags replace the existing tag list. Run any command with `--help`
for details. Output is JSON with post identifiers and editor links.

## Agent skill and tests

Install this folder as the `ghost` skill or point an agent at [SKILL.md](SKILL.md).
Keep personal writing rules and site deployment instructions outside this repo.

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic credentials, local fixtures and mocked Ghost requests.
They do not change a real blog.

[MIT](LICENSE). An independent client for the
[Ghost Admin API](https://docs.ghost.org/admin-api).
