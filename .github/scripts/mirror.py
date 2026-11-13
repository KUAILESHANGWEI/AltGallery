#!/usr/bin/env python3
"""Localize the catalog and retain immutable release assets across updates."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[2]
TARGET = 'KUAILESHANGWEI/AltGallery'
UPSTREAM = 'bebound/AltGallery'
MANIFEST = ROOT / 'archive-manifest.json'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def api(path):
    return json.loads(subprocess.check_output(['gh', 'api', path]))


def localize():
    files = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    for name in files:
        path = ROOT / name
        if name.startswith('.github/scripts/') or name in ('.github/workflows/update.yml', 'archive-manifest.json') or not path.is_file():
            continue
        try:
            original = path.read_text()
        except (UnicodeError, OSError):
            continue
        text = original.replace('bebound/AltGallery/refs/heads/master', TARGET + '/refs/heads/main')
        text = text.replace('bebound/AltGallery/master', TARGET + '/refs/heads/main')
        text = text.replace('bebound/AltGallery', TARGET)
        text = text.replace('bebound/altgallery', TARGET)
        text = text.replace('bebound%2Faltgallery', 'KUAILESHANGWEI%2FAltGallery')
        text = text.replace('bebound.github.io/AltGallery', 'KUAILESHANGWEI.github.io/AltGallery')
        text = text.replace('altgallery.fromkk.com', 'KUAILESHANGWEI.github.io/AltGallery')
        if name.startswith('.github/workflows/'):
            text = re.sub(r'(?m)^(\s+- )master$', r'\1main', text)
        if text != original:
            path.write_text(text)


def release(tag):
    result = subprocess.run(['gh', 'release', 'view', tag, '-R', TARGET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        run('gh', 'release', 'create', tag, '-R', TARGET, '--target', 'main', '--title', tag, '--notes', 'Retained source assets. Existing assets are never removed.')


def archive():
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    sources = sorted(ROOT.glob('apps/*/apps.json'))
    catalogs = {p: json.loads(p.read_text()) for p in sources}
    inverse = {item['url']: source for source, item in manifest.items()}
    urls = set()
    for data in catalogs.values():
        for app in data['apps']:
            for version in app.get('versions', []):
                url = version['downloadURL']
                urls.add(inverse.get(url, url))
    # Archive every asset of the latest upstream release when one exists.
    try:
        upstream_releases = api(f'repos/{UPSTREAM}/releases?per_page=1')
    except subprocess.CalledProcessError:
        upstream_releases = []
        print('Upstream releases unavailable; keeping existing archives.', flush=True)
    if upstream_releases:
        urls.update(asset['browser_download_url'] for asset in upstream_releases[0]['assets'])
    release('archive-assets')
    existing = {a['name']: a for a in api(f'repos/{TARGET}/releases/tags/archive-assets')['assets']}

    def save(url):
        old = manifest.get(url)
        source_digest = None
        match = re.match(r'https://github.com/([^/]+/[^/]+)/releases/download/([^/]+)/(.+)$', url)
        if match:
            repo, tag, filename = match.groups()
            try:
                upstream_release = api(f'repos/{repo}/releases/tags/{quote(unquote(tag), safe="")}')
                source_asset = next(a for a in upstream_release['assets'] if a['name'] == unquote(filename))
                source_digest = source_asset.get('digest')
            except (subprocess.CalledProcessError, StopIteration):
                if not old:
                    raise RuntimeError('Source unavailable and no retained copy: ' + url)
                source_digest = 'sha256:' + old['sha256']
        if old and old['name'] in existing:
            remote = existing[old['name']]
            if remote['size'] == old['size'] and remote.get('digest') == 'sha256:' + old['sha256'] and source_digest == 'sha256:' + old['sha256']:
                return url, old
        with tempfile.TemporaryDirectory(prefix='altgallery-') as folder:
            path = Path(folder) / 'download'
            run('curl', '--fail', '--location', '--retry', '4', '--silent', '--show-error', '--output', str(path), url)
            with path.open('rb') as stream:
                checksum = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    checksum.update(chunk)
            digest = checksum.hexdigest()
            if source_digest and source_digest != 'sha256:' + digest:
                raise RuntimeError('Source asset SHA-256 mismatch: ' + url)
            basename = re.sub(r'[^A-Za-z0-9._+-]', '_', unquote(url.rsplit('/', 1)[-1]))
            name = hashlib.sha256(url.encode()).hexdigest()[:12] + '-' + digest[:12] + '-' + basename
            asset = Path(folder) / name
            path.rename(asset)
            if name not in existing:
                run('gh', 'release', 'upload', 'archive-assets', str(asset), '-R', TARGET)
            return url, {'name': name, 'url': f'https://github.com/{TARGET}/releases/download/archive-assets/{quote(name)}', 'sha256': digest, 'size': asset.stat().st_size}

    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(save, url): url for url in urls}
        for job in concurrent.futures.as_completed(jobs):
            try:
                source, entry = job.result()
                manifest[source] = entry
                temporary = MANIFEST.with_suffix('.tmp')
                temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
                temporary.replace(MANIFEST)
                print('Archived', entry['name'], flush=True)
            except Exception as error:
                failures.append(jobs[job])
                print('Archive failed', jobs[job], str(error), flush=True)
    if failures:
        raise SystemExit(f'{len(failures)} assets failed; successful archives are retained. Retry to continue.')
    # Check remote bytes before publishing any catalog URL.
    remote = {a['name']: a for a in api(f'repos/{TARGET}/releases/tags/archive-assets')['assets']}
    for url in urls:
        entry = manifest[url]
        asset = remote[entry['name']]
        if asset['size'] != entry['size'] or asset.get('digest') != 'sha256:' + entry['sha256']:
            raise SystemExit('Remote checksum mismatch: ' + entry['name'])
    for upstream_release in upstream_releases:
        tag = upstream_release['tag_name']
        release(tag)
        retained = {a['name']: a for a in api(f'repos/{TARGET}/releases/tags/{quote(tag, safe="")}')['assets']}
        for source_asset in upstream_release['assets']:
            entry = manifest[source_asset['browser_download_url']]
            name = source_asset['name']
            old = retained.get(name)
            if old and old.get('digest') == 'sha256:' + entry['sha256']:
                continue
            if old:
                name = entry['sha256'][:12] + '-' + name
            if name in retained:
                continue
            with tempfile.TemporaryDirectory(prefix='altgallery-release-') as folder:
                run('gh', 'release', 'download', 'archive-assets', '-R', TARGET, '--pattern', entry['name'], '--dir', folder)
                asset = Path(folder) / entry['name']
                destination = Path(folder) / name
                asset.rename(destination)
                run('gh', 'release', 'upload', tag, str(destination), '-R', TARGET)
    for path, data in catalogs.items():
        for app in data['apps']:
            for version in app.get('versions', []):
                source = inverse.get(version['downloadURL'], version['downloadURL'])
                version['downloadURL'] = manifest[source]['url']
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    (ROOT / 'SHA256SUMS').write_text(''.join(f"{item['sha256']}  {item['name']}\n" for item in sorted(manifest.values(), key=lambda x: x['name'])))


if __name__ == '__main__':
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument('--localize-only', action='store_true')
    args = parser.parse_args()
    localize()
    if not args.localize_only:
        archive()
