"""Read-only check. Never prints private-key contents or credential values."""
import base64
import configparser
import hashlib
import io
import json
import pathlib
import re
import tarfile

ROOT=pathlib.Path(__file__).resolve().parent
config=configparser.ConfigParser()
config.read(pathlib.Path.home()/'.oci/config')
key_path=pathlib.Path(config['DEFAULT']['key_file']).expanduser()
private_key=key_path.read_bytes()
match=re.search(rb'-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----\s*(.*?)\s*-----END (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----',private_key,re.S)
if not match:raise RuntimeError('Cannot identify the configured private-key format; audit incomplete')
body=re.sub(rb'\s+',b'',match.group(1))
der=base64.b64decode(body)
needles=[private_key.strip(),body,der]+[body[i:i+128] for i in range(0,len(body)-127,128)]
findings=[]
def check(name,data):
    if any(value in data for value in needles):findings.append({'path':name,'issue':'Configured API private-key material detected'})
    if str(key_path).encode() in data:findings.append({'path':name,'issue':'Configured local private-key path detected'})

source_count=0
for path in ROOT.rglob('*'):
    if not path.is_file() or any(part in ('build','tools','__pycache__','.git') for part in path.relative_to(ROOT).parts):continue
    check('source/'+str(path.relative_to(ROOT)),path.read_bytes());source_count+=1

layout=ROOT/'build/image-layout'
index=json.loads((layout/'index.json').read_text())
manifest_digest=index['manifests'][0]['digest']
def read_blob(digest):
    data=(layout/'blobs/sha256'/digest.split(':',1)[1]).read_bytes()
    if 'sha256:'+hashlib.sha256(data).hexdigest()!=digest:raise RuntimeError('Image blob integrity check failed')
    return data
manifest=json.loads(read_blob(manifest_digest))
image_config=read_blob(manifest['config']['digest']);check('image/config',image_config)
image_files=0
app_files={}
for number,layer in enumerate(manifest['layers']):
    raw=read_blob(layer['digest'])
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:*') as archive:
        for member in archive:
            name=member.name.lstrip('./')
            if '.oci' in pathlib.PurePosixPath(name).parts or name.endswith('/oci_local_access.py'):
                findings.append({'path':f'image/layer-{number}/{name}','issue':'Workstation credential path or helper included'})
            if not member.isfile():continue
            contents=archive.extractfile(member).read();image_files+=1
            check(f'image/layer-{number}/{name}',contents)
            if name in ('function/func.py','function/index.html','function/requirements.txt'):app_files[name]=contents
for name,contents in app_files.items():
    if contents!=(ROOT/name).read_bytes():findings.append({'path':name,'issue':'Packaged application differs from reviewed source'})
if len(app_files)!=3:findings.append({'issue':'Application file allowlist is incomplete'})
report={'configured_private_key_matches':sum(x.get('issue')=='Configured API private-key material detected' for x in findings),
        'source_files_checked':source_count,'image_files_checked':image_files,'image_layers_checked':len(manifest['layers']),
        'application_source_matches_image':len(app_files)==3 and all(contents==(ROOT/name).read_bytes() for name,contents in app_files.items()),
        'findings':findings}
print(json.dumps(report,indent=2))
raise SystemExit(1 if findings else 0)
