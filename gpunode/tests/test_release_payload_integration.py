import importlib.util, json, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path
import os, pytest
ROOT=Path(__file__).parents[2]; GP=ROOT/'gpunode'
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
builder=load('builder',GP/'scripts'/'build_node_release.py');rc=load('rc_payload',GP/'release_channel.py')
def test_first_payload_build_is_exact_and_cli_is_unambiguous():
 with tempfile.TemporaryDirectory() as t:
  t=Path(t);out=t/'out';p,m=builder.build_release(ROOT,out,'1.2.3','test',b'test-only-key',list(builder.FIRST_PAYLOAD_FILES),'https://releases.example/p.zip')
  assert tuple(json.loads(m.read_text())['files'])==builder.FIRST_PAYLOAD_FILES
  with zipfile.ZipFile(p) as z:assert tuple(z.namelist())==builder.FIRST_PAYLOAD_FILES
  key=t/'key';key.write_bytes(b'x');r=subprocess.run([sys.executable,str(GP/'scripts'/'build_node_release.py'),'--source-root',str(ROOT),'--output-dir',str(out),'--version','1.2.3','--source-revision','x','--package-url','https://releases.example/p','--hmac-key-file',str(key),'--first-payload','--include','gpunode/node_jobs.py'],capture_output=True,text=True);assert r.returncode
def test_payload_closure_and_protected_paths_are_declared():
 names=set(builder.FIRST_PAYLOAD_FILES)
 assert {'gpunode/node_jobs.py','gpunode/legacy_artifact_backfill.py','gpunode/model_inventory.py','gpunode/stages/router.py','gpunode/stages/diarize_node.py','gpunode/stages/offline.py','gpunode/stages/demo.py','gpunode/stages/tts_node.py','gpunode/stages/separate_node.py'}<=names
 assert not any(x in n for n in names for x in ('entrypoint','workdir','models','tests','scripts','release_channel'))
@pytest.mark.parametrize('mode',['offline','real'])
def test_installed_payload_real_entrypoint_probe(mode):
 with tempfile.TemporaryDirectory() as t:
  t=Path(t);local=t/'local'/'gpunode';local.mkdir(parents=True);shutil.copy2(GP/'entrypoint.py',local/'entrypoint.py');work=local/'workdir';work.mkdir();(work/'sentinel').write_text('keep');models=local/'models';models.mkdir();(models/'manifest.json').write_text('{}');(models/'sentinel').write_text('keep')
  p,m=builder.build_release(ROOT,t/'out','1.2.3','test',b'test-only-key',list(builder.FIRST_PAYLOAD_FILES),'https://releases.example/p.zip');manifest=json.loads(m.read_text());rc.validate_manifest(manifest,b'test-only-key',{'releases.example'})
  state=rc.StateClient('https://control',work/'node_token.txt',lambda *a:{}) ;sup=rc.ReleaseSupervisor(t/'release',local/'entrypoint.py',state);sup.install(manifest,p.read_bytes());release=sup.releases/'1.2.3'
  code="import os,sys,runpy,json;sys.path[:0]=[os.environ['REL']+'/gpunode',os.environ['LOC']];n=runpy.run_path(os.environ['LOC']+'/entrypoint.py',run_name='_release_probe');import node_jobs,legacy_artifact_backfill,model_inventory,stages,stages.router,stages.diarize_node as d;print(json.dumps({'entry':n['__file__'],'node':node_jobs.__file__,'legacy':legacy_artifact_backfill.__file__,'model':model_inventory.__file__,'router':stages.router.__file__,'diarize':d.__file__,'work':d.WORKDIR,'token':d.TOKEN_FILE,'manifest':str(model_inventory.DEFAULT_MANIFEST_PATH)}))"
  env=dict(os.environ,REL=str(release),LOC=str(local),NODE_WORKDIR=str(work),NODE_MODEL_MANIFEST=str(models/'manifest.json'),NODE_MODE=mode)
  out=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,check=True).stdout;data=json.loads(out)
  assert data['entry']==str(local/'entrypoint.py') and all(data[k].startswith(str(release/'gpunode')) for k in ('node','legacy','model','router','diarize'))
  assert data['work']==str(work) and data['token']==str(work/'node_token.txt') and data['manifest']==str(models/'manifest.json') and (work/'sentinel').read_text()=='keep' and (models/'sentinel').read_text()=='keep'
