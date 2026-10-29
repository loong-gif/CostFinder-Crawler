"""Recover the existing PG16 base image without changing daemon-wide proxies."""
import concurrent.futures
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

import requests

root = Path('/vol1/firecrawl_recovery_backups/postgres16_image')
root.mkdir(parents=True, exist_ok=True)
base = 'https://docker.m.daocloud.io/v2/library/postgres'
accept = ','.join(['application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json', 'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])
session = requests.Session()
session.trust_env = False
response = session.get(base + '/manifests/16', headers={'Accept': accept}, timeout=30)
challenge = dict(re.findall(r'(\w+)="([^"]+)"', response.headers.get('WWW-Authenticate', '')))
token_response = session.get(challenge['realm'], params={k: v for k, v in challenge.items() if k != 'realm'}, timeout=30)
token_response.raise_for_status()
tokens = token_response.json()
headers = {'Authorization': 'Bearer ' + (tokens.get('token') or tokens['access_token']), 'Accept': accept}
index_digest = 'sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d'
response = session.get(base + '/manifests/' + index_digest, headers=headers, timeout=30)
response.raise_for_status()
if 'sha256:' + hashlib.sha256(response.content).hexdigest() != index_digest:
    raise RuntimeError('index_integrity_failed')
index = response.json()
platform = next(x for x in index['manifests'] if x.get('platform', {}).get('architecture') == 'amd64' and x.get('platform', {}).get('os') == 'linux')
response = session.get(base + '/manifests/' + platform['digest'], headers=headers, timeout=30)
response.raise_for_status()
if 'sha256:' + hashlib.sha256(response.content).hexdigest() != platform['digest']:
    raise RuntimeError('manifest_integrity_failed')
manifest = response.json()


def download(entry):
    digest = entry['digest']
    path = root / (digest.split(':')[1] + '.blob')
    client = requests.Session()
    client.trust_env = False
    response = client.get(base + '/blobs/' + digest, headers=headers, stream=True, timeout=60)
    response.raise_for_status()
    check = hashlib.sha256()
    size = 0
    with path.open('wb') as output:
        for chunk in response.iter_content(1024 * 1024):
            output.write(chunk)
            check.update(chunk)
            size += len(chunk)
    response.close()
    if check.hexdigest() != digest.split(':')[1] or size != entry['size']:
        raise RuntimeError('image_blob_integrity_failed')
    print(json.dumps({'blob': digest[:19], 'bytes': size}), flush=True)
    return path


entries = [manifest['config']] + manifest['layers']
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    paths = list(pool.map(download, entries))
config = json.loads(paths[0].read_bytes())
if config.get('architecture') != 'amd64' or 'PG_MAJOR=16' not in config.get('config', {}).get('Env', []):
    raise RuntimeError('wrong_postgres_image_version')
config_name = manifest['config']['digest'].split(':')[1] + '.json'
layers = []
archive = root / 'postgres16.tar'
with tarfile.open(archive, 'w') as output:
    content = paths[0].read_bytes()
    info = tarfile.TarInfo(config_name)
    info.size = len(content)
    output.addfile(info, io.BytesIO(content))
    for number, path in enumerate(paths[1:]):
        layer = root / (str(number) + '_layer.tar')
        check = hashlib.sha256()
        with gzip.open(path, 'rb') as source, layer.open('wb') as destination:
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
                check.update(chunk)
        if 'sha256:' + check.hexdigest() != config['rootfs']['diff_ids'][number]:
            raise RuntimeError('layer_diff_id_failed')
        name = str(number) + '/layer.tar'
        layers.append(name)
        output.add(layer, arcname=name)
    content = json.dumps([{'Config': config_name, 'RepoTags': ['postgres:16'], 'Layers': layers}]).encode()
    info = tarfile.TarInfo('manifest.json')
    info.size = len(content)
    output.addfile(info, io.BytesIO(content))
print(json.dumps({'archive_ready': str(archive), 'bytes': archive.stat().st_size}), flush=True)
subprocess.run(['docker', 'load', '-i', str(archive)], check=True)
print('postgres16_image_loaded', flush=True)
