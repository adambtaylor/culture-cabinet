"""Exact album artwork transfer and opaque sidecar verification over existing SSH.

The source stays inside the established SAB Complete root. CUE is hash-only and
never copied, parsed or executed. No account access, provider fetch or playback.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import tempfile
from uuid import UUID

import mac_kodi_consumer as safe

REMOTE_ROOT = '/home/tang/Downloads/Usenet/Complete'
IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png'}
MAX_IMAGE_BYTES = 20 * 1024**2
MAX_CUE_BYTES = 1024**2

REMOTE_PROGRAM = r'''
import hashlib,json,os,stat,subprocess,sys
root,rel,limit,copy=sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]=='copy'
parts=rel.split('/')
if rel.startswith('/') or any(p in ('','.','..') for p in parts):
    raise SystemExit(1)
fds=[]
try:
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY);fds.append(fd)
    for part in root.strip('/').split('/')+parts[:-1]:
        fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(fd)
    src=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd);fds.append(src)
    before=os.fstat(src)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or not 0<before.st_size<=limit:
        raise SystemExit(1)
    codec=None
    if copy:
        header=os.read(src,8);os.lseek(src,0,0)
        suffix=os.path.splitext(rel)[1].lower()
        if suffix=='.png':
            if header!=b'\x89PNG\r\n\x1a\n':raise SystemExit(1)
        elif suffix in ('.jpg','.jpeg'):
            if not header.startswith(b'\xff\xd8\xff'):raise SystemExit(1)
        else:raise SystemExit(1)
        result=subprocess.run(['ffprobe','-v','error','-show_entries','stream=codec_type,codec_name',
            '-of','json','/proc/self/fd/'+str(src)],pass_fds=(src,),capture_output=True,timeout=10)
        if result.returncode or len(result.stdout)>65536:raise SystemExit(1)
        streams=json.loads(result.stdout).get('streams',[])
        codec='png' if suffix=='.png' else 'mjpeg'
        if len(streams)!=1 or streams[0].get('codec_type')!='video' or streams[0].get('codec_name')!=codec:
            raise SystemExit(1)
    digest=hashlib.sha256();count=0;os.lseek(src,0,0)
    while True:
        block=os.read(src,1024*1024)
        if not block:break
        count+=len(block)
        if count>limit:raise SystemExit(1)
        digest.update(block)
        if copy:sys.stdout.buffer.write(block)
    sys.stdout.buffer.flush()
    after=os.fstat(src)
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or count!=before.st_size:
        raise SystemExit(1)
    print(json.dumps({'bytes':count,'sha256':digest.hexdigest(),'image_codec':codec}),file=sys.stderr)
finally:
    for fd in reversed(fds):os.close(fd)
'''


def relative_path(value, *, art=False):
    safe.require(type(value) is str and 0 < len(value) <= 4096 and '\\' not in value
        and not any(ord(c) < 32 for c in value) and not value.startswith('/'), 'Invalid sidecar path.')
    parts = value.split('/')
    safe.require(2 <= len(parts) <= 16 and not any(p in ('', '.', '..') for p in parts), 'Unsafe sidecar path.')
    safe.require(parts[0].startswith('CC-') and str(UUID(parts[0][3:])) == parts[0][3:], 'Exact request folder required.')
    path = PurePosixPath(value)
    allowed = IMAGE_SUFFIXES if art else IMAGE_SUFFIXES | {'.cue'}
    safe.require(path.suffix.lower() in allowed, 'Unsupported sidecar.')
    return path


def _read(relative, *, expected_sha256, expected_bytes, copy, target=None, runner=subprocess.run):
    path = relative_path(relative, art=copy)
    limit = MAX_CUE_BYTES if path.suffix.lower() == '.cue' else MAX_IMAGE_BYTES
    safe.require(safe.is_hash(expected_sha256) and type(expected_bytes) is int
        and 0 < expected_bytes <= limit, 'Bounded expected sidecar bytes required.')
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
        '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2', 'omarchy',
        shlex.join(['python3', '-I', '-S', '-c', REMOTE_PROGRAM, REMOTE_ROOT,
                    str(path), str(limit), 'copy' if copy else 'verify'])]
    with tempfile.TemporaryFile() as output:
        result = runner(command, stdout=output, stderr=subprocess.PIPE, timeout=30, check=False)
        safe.require(result.returncode == 0 and len(result.stderr) <= 65536, 'Sidecar unavailable.')
        info = json.loads(result.stderr, object_pairs_hook=safe.helpers()[0].unique_object)
        safe.require(type(info) is dict and type(info.get('bytes')) is int
            and info['bytes'] == expected_bytes and info.get('sha256') == expected_sha256,
            'Sidecar changed since completion.')
        if not copy:
            safe.require(output.tell() == 0, 'Unexpected sidecar payload.')
            return {'sha256': expected_sha256, 'bytes': expected_bytes}
        safe.require(info.get('image_codec') == ('png' if path.suffix.lower() == '.png' else 'mjpeg')
            and output.tell() == expected_bytes, 'Invalid image evidence.')
        output.seek(0)
        digest = hashlib.sha256()
        with open(target, 'xb') as image:
            for block in iter(lambda: output.read(1024**2), b''):
                digest.update(block); image.write(block)
            image.flush(); os.fsync(image.fileno())
        safe.require(digest.hexdigest() == expected_sha256, 'Image bytes differ.')
        with open(target, 'rb') as image:
            header = image.read(8)
        safe.require(header == b'\x89PNG\r\n\x1a\n' if path.suffix.lower() == '.png'
            else header.startswith(b'\xff\xd8\xff'), 'Image header differs.')
        return {'local_path': str(target), 'sha256': expected_sha256, 'bytes': expected_bytes}


def fetch_art(relative, staging, *, expected_sha256, expected_bytes, runner=subprocess.run):
    path = relative_path(relative, art=True)
    staging = safe.safe_directory(staging)
    target = staging.joinpath(*path.parts)
    safe.safe_directory(target.parent, create=True)
    # Exclusive staging creation never replaces an existing file. The caller's
    # private staging lifetime owns any partial file after verification fails.
    return _read(relative, expected_sha256=expected_sha256, expected_bytes=expected_bytes,
                 copy=True, target=target, runner=runner)


def verify_sidecar(relative, *, expected_sha256, expected_bytes, runner=subprocess.run):
    return _read(relative, expected_sha256=expected_sha256, expected_bytes=expected_bytes,
                 copy=False, runner=runner)
