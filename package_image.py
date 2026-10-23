"""Package an OCI image locally. --push is a separate, explicit deployment action."""
import sys, argparse, copy, gzip, hashlib, json, pathlib, tarfile, urllib.parse, subprocess
from oci_local_access import config
import oci
from oci._vendor import requests

ROOT=pathlib.Path(__file__).parent
LAYOUT=ROOT/'build/image-layout'
def digest(data):return 'sha256:'+hashlib.sha256(data).hexdigest()
def blob(data):
    value=digest(data);path=LAYOUT/'blobs/sha256'/value.split(':')[1];path.write_bytes(data)
    return value,len(data)
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--push',action='store_true');args=parser.parse_args()
    (LAYOUT/'blobs/sha256').mkdir(parents=True,exist_ok=True)
    base=json.loads((ROOT/'base-manifest.json').read_text())
    baseconfig=json.loads((ROOT/'base-config.json').read_text())
    session=requests.Session()
    token_response=session.get('https://auth.docker.io/token',params={'service':'registry.docker.io','scope':'repository:fnproject/python:pull'},timeout=30);token_response.raise_for_status()
    headers={'Authorization':'Bearer '+token_response.json()['token']}
    for layer in base['layers']:
        dest=LAYOUT/'blobs/sha256'/layer['digest'].split(':')[1]
        if dest.exists() and digest(dest.read_bytes())==layer['digest']:continue
        result=session.get('https://registry-1.docker.io/v2/fnproject/python/blobs/'+layer['digest'],headers=headers,timeout=90);result.raise_for_status()
        if digest(result.content)!=layer['digest']:raise RuntimeError('Base layer checksum mismatch')
        dest.write_bytes(result.content);print('Verified official runtime layer',layer['digest'][:24],flush=True)
    raw_layer=ROOT/'build/function-layer.tar'
    def normalize(info):
        if '__pycache__' in info.name:return None
        info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0
        info.mode=0o755 if info.isdir() else 0o644
        return info
    with tarfile.open(raw_layer,'w') as archive:
        archive.add(ROOT/'build/python',arcname='python',filter=normalize)
        for name in ['func.py','index.html','requirements.txt']:
            source=ROOT/'function'/name
            if b'-----BEGIN PRIVATE KEY-----' in source.read_bytes() or b'-----BEGIN RSA PRIVATE KEY-----' in source.read_bytes():raise RuntimeError('Unexpected key material')
            archive.add(source,arcname='function/'+name,filter=normalize)
    raw=raw_layer.read_bytes();compressed=gzip.compress(raw,compresslevel=6,mtime=0)
    layer_digest,layer_size=blob(compressed)
    image=copy.deepcopy(baseconfig)
    image['config'].update(WorkingDir='/function',Entrypoint=['python3.11','/python/bin/fdk','/function/func.py','handler'],Cmd=[],User='1000:1000')
    image['config']['Env']=image['config'].get('Env',[])+['PYTHONPATH=/function:/python','PYTHONDONTWRITEBYTECODE=1']
    image['rootfs']['diff_ids'].append(digest(raw))
    image.setdefault('history',[]).append({'created_by':'OCI Limits Dashboard: pinned dependencies and application source'})
    config_digest,config_size=blob(json.dumps(image,separators=(',',':')).encode())
    manifest={'schemaVersion':2,'mediaType':'application/vnd.docker.distribution.manifest.v2+json',
        'config':{'mediaType':'application/vnd.docker.container.image.v1+json','size':config_size,'digest':config_digest},
        'layers':base['layers']+[{'mediaType':'application/vnd.docker.image.rootfs.diff.tar.gzip','size':layer_size,'digest':layer_digest}]}
    manifest_bytes=json.dumps(manifest,separators=(',',':')).encode();manifest_digest,manifest_size=blob(manifest_bytes)
    (LAYOUT/'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
    (LAYOUT/'index.json').write_text(json.dumps({'schemaVersion':2,'manifests':[{'mediaType':manifest['mediaType'],'digest':manifest_digest,'size':manifest_size,'platform':{'os':'linux','architecture':'amd64'}}]}))
    metadata={'digest':manifest_digest,'source_files':['function/func.py','function/index.html','function/requirements.txt'],'architecture':'linux/amd64','layer_size_bytes':layer_size}
    (ROOT/'build/image-metadata.json').write_text(json.dumps(metadata,indent=2))
    print('Local image prepared',metadata,flush=True)
    # Stop before any registry upload if the local private key or config was included.
    subprocess.run([sys.executable,str(ROOT/'check_credentials.py')],cwd=ROOT,check=True)
    if not args.push:return
    state=json.loads((ROOT/'deployment.json').read_text());repository=state['image_repository'].split('/',1)[1]
    registry='https://iad.ocir.io'
    signer=oci.signer.Signer(tenancy=config['tenancy'],user=config['user'],fingerprint=config['fingerprint'],private_key_file_location=config['key_file'],pass_phrase=config.get('pass_phrase'))
    auth=session.get(registry+'/20180419/docker/token',params={'service':'iad.ocir.io','scope':'repository:'+repository+':pull,push'},auth=signer,timeout=30);auth.raise_for_status()
    token=auth.json().get('token') or auth.json().get('access_token')
    if not token:raise RuntimeError('No registry access token returned')
    push_headers={'Authorization':'Bearer '+token}
    for item in [manifest['config']]+manifest['layers']:
        target=registry+'/v2/'+repository+'/blobs/'+item['digest']
        if session.head(target,headers=push_headers,timeout=30).status_code==200:continue
        start=session.post(registry+'/v2/'+repository+'/blobs/uploads/',headers=push_headers,timeout=30);start.raise_for_status()
        location=urllib.parse.urljoin(registry,start.headers['Location'])
        if urllib.parse.urlparse(location).hostname!='iad.ocir.io':raise RuntimeError('Unexpected registry upload host')
        separator='&' if '?' in location else '?'
        data=(LAYOUT/'blobs/sha256'/item['digest'].split(':')[1]).read_bytes()
        result=session.put(location+separator+'digest='+urllib.parse.quote(item['digest']),headers={**push_headers,'Content-Type':'application/octet-stream'},data=data,timeout=180);result.raise_for_status()
        print('Uploaded image layer',item['digest'][:24],flush=True)
    tag='20261023-v1'
    pushed=session.put(registry+'/v2/'+repository+'/manifests/'+tag,headers={**push_headers,'Content-Type':manifest['mediaType']},data=manifest_bytes,timeout=30);pushed.raise_for_status()
    state.update(image=state['image_repository']+':'+tag,image_digest=manifest_digest)
    (ROOT/'deployment.json').write_text(json.dumps(state,indent=2))
    print('Private function image pushed',state['image'],flush=True)
if __name__=='__main__':main()
