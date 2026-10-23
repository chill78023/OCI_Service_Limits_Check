import sys,json,pathlib
from oci_local_access import *
from oci._vendor import requests
accept='application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json'
s=requests.Session()
token=s.get('https://auth.docker.io/token',params={'service':'registry.docker.io','scope':'repository:fnproject/python:pull'},timeout=20).json()['token']
h={'Authorization':'Bearer '+token,'Accept':accept}
r=s.get('https://registry-1.docker.io/v2/fnproject/python/manifests/3.11',headers=h,timeout=20);r.raise_for_status();m=r.json()
if 'manifests' in m:
    digest=next(x['digest'] for x in m['manifests'] if x.get('platform',{}).get('architecture')=='amd64')
    r=s.get('https://registry-1.docker.io/v2/fnproject/python/manifests/'+digest,headers=h,timeout=20);r.raise_for_status();m=r.json()
else:digest=r.headers['Docker-Content-Digest']
c=s.get('https://registry-1.docker.io/v2/fnproject/python/blobs/'+m['config']['digest'],headers=h,timeout=20);c.raise_for_status()
pathlib.Path('base-manifest.json').write_text(json.dumps(m));pathlib.Path('base-config.json').write_text(c.text)
print('base_digest',digest,'config',c.json().get('config'))
