import sys,json,urllib.request,pathlib
import oci_local_access
requirements=pathlib.Path('function/requirements.txt')
from pip._internal.cli.main import main
raise SystemExit(main(['install','--target','build/python','--platform','manylinux2014_x86_64','--platform','manylinux_2_28_x86_64','--python-version','3.11','--implementation','cp','--abi','cp311','--only-binary=:all:','--disable-pip-version-check','--no-compile','-r',str(requirements)]))
