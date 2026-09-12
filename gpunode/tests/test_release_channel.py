import hashlib,hmac,importlib.util,io,json,zipfile
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location("rc",Path(__file__).parents[1]/"release_channel.py");rc=importlib.util.module_from_spec(spec);spec.loader.exec_module(rc)
KEY=b"k"; HOSTS={"releases.example"}
def pkg(files={"gpunode/node_jobs.py":b"ok"}):
 b=io.BytesIO()
 with zipfile.ZipFile(b,"w") as z:
  for n,v in files.items():z.writestr(n,v)
 p=b.getvalue();m={"version":"1.2.3","source_revision":"x","package_url":"https://releases.example/a.zip","package_sha256":hashlib.sha256(p).hexdigest(),"files":{n:hashlib.sha256(v).hexdigest() for n,v in files.items()}}
 m["signature"]=hmac.new(KEY,rc.canonical_manifest(m),hashlib.sha256).hexdigest();return p,m
@pytest.mark.parametrize("n",["..\\evil.py","C:\\evil.py","\\\\server\\x","dir\\entrypoint.py","a:x.py","../x","gpunode/entrypoint.py"])
def test_windows_paths_rejected(n):
 with pytest.raises(rc.ReleaseError):rc.safe_member_name(n)
def test_zip_expansion_and_redirect_final_url(tmp_path):
 p,m=pkg();m["files"]={"gpunode/a.py":"0"*64,"gpunode/b.py":"0"*64};m["signature"]=hmac.new(KEY,rc.canonical_manifest(m),hashlib.sha256).hexdigest()
 with pytest.raises(rc.ReleaseError):rc.validate_package(p,m,tmp_path/"x")
 class R:
  def __enter__(s):return s
  def __exit__(s,*x):pass
  def geturl(s):return "https://evil.example/x"
  def read(s,n):return b""
 with pytest.raises(rc.ReleaseError):rc.download_limited("https://releases.example/x",HOSTS,lambda *x,**k:R())
 class H(rc.AllowlistedRedirect): pass
 with pytest.raises(rc.ReleaseError):H(HOSTS).redirect_request(None,None,302,"",None,"https://evil.example/x")
class P:
 def __init__(s,dead=False):s.dead=dead;s.terminated=False
 def poll(s):return 1 if s.dead else None
 def terminate(s):s.terminated=True;s.dead=True
class T:
 def __init__(s):s.calls=[];s.ready=True
 def __call__(s,*a):s.calls.append(a);return {"ready_to_switch":s.ready,"running_pipeline_tasks":0 if s.ready else 1,"running_node_jobs":0}
def sup(tmp,procs):
 tmp.mkdir(parents=True,exist_ok=True);token=tmp/"token";token.write_text("resident") ;entry=tmp/"entry.py";entry.write_text("pass");t=T();it=iter(procs);return rc.ReleaseSupervisor(tmp/"r",entry,rc.StateClient("https://c",token,t,clock=lambda:0,sleeper=lambda x:None),popen=lambda *a,**k:next(it),clock=lambda:0,sleeper=lambda x:None),t
def test_same_manifest_one_launch_overlay_and_stop_before_switch(tmp_path):
 s,t=sup(tmp_path,[P(),P()]);p,m=pkg();assert s.update(m,p,KEY,HOSTS,health_seconds=0);assert s.update(m,p,KEY,HOSTS,health_seconds=0)
 s3,t3=sup(tmp_path/"overlay",[P()]);s3.install(m,p);seen=[];s3.popen=lambda argv,**kw:seen.append(argv) or P();s3.launch("1.2.3");assert "releases/1.2.3/gpunode" in seen[0][2]
 calls=[];s2,t2=sup(tmp_path/"b",[P()]);s2.releases.mkdir(parents=True);(s2.releases/"1.0.0").mkdir();(s2.releases/"1.0.0"/".release-meta.json").write_text(json.dumps({"digest":"a"*64}));s2.write_pointer({"version":"1.0.0","digest":"a"*64},None);old=P();s2.child=old;s2.popen=lambda *a,**k:(calls.append(old.terminated) or P());assert s2.update(m,p,KEY,HOSTS,health_seconds=0);assert calls==[True]
def test_empty_timeout_and_initial_health_failure_clear_pointer(tmp_path):
 s,t=sup(tmp_path,[P(True)]);p,m=pkg();t.ready=False;assert s.update(m,p,KEY,HOSTS,drain_timeout=0,health_seconds=0) is False;assert t.calls[0][3]["ready"] is False
 s,t=sup(tmp_path/"x",[P(True)]);p,m=pkg()
 with pytest.raises(rc.ReleaseError):s.update(m,p,KEY,HOSTS,health_seconds=1)
 assert rc.pointer(s.path)["current"] is None
def test_installer_single_task_staging_and_bootstrap_layout():
 text=(Path(__file__).parents[1]/"scripts"/"install_release_channel.ps1").read_text()
 for needle in ("ResidentTaskName","/Query","/End",".release-stage-","Move-Item","/XML","$LASTEXITCODE"):assert needle in text
 assert "Start-Process" not in text
def test_stop_timeout_preserves_old_and_does_not_launch(tmp_path):
 class Stuck(P):
  def terminate(self): self.terminated=True
 old=Stuck();s,t=sup(tmp_path,[P()]);s.releases.mkdir(parents=True);(s.releases/"1.0.0").mkdir();s.write_pointer({"version":"1.0.0","digest":"a"*64},None);s.child=old;p,m=pkg();launch=[];s.popen=lambda *a,**k:launch.append(a) or P()
 with pytest.raises(rc.ReleaseError):s.update(m,p,KEY,HOSTS,health_seconds=0)
 assert s.child is old and rc.pointer(s.path)["current"]["version"]=="1.0.0" and not launch and t.calls[-1][3]["ready"] is True
def test_installer_activation_guard_order():
 text=(Path(__file__).parents[1]/"scripts"/"install_release_channel.ps1").read_text()
 assert "$taskXml=Join-Path $parent" in text and "$activatedNew=$false" in text
 assert text.index("Move-Item -LiteralPath $stage -Destination $InstallRoot; $activatedNew=$true") < text.index("if($activatedNew -and") < text.index("if($moved){Move-Item")
def test_installer_commit_cleanup_is_outside_rollback():
 text=(Path(__file__).parents[1]/"scripts"/"install_release_channel.ps1").read_text()
 run=text.index("Invoke-Schtasks @('/Run'"); commit=text.index("$committed=$true"); rollback=text.index("if(-not $committed)"); cleanup=text.rindex("try { if($committed")
 assert run < commit < rollback < cleanup
def test_watch_ensures_before_each_update_and_restarts_only_dead_child(tmp_path):
 key=tmp_path/'key';key.write_text('x');entry=tmp_path/'entry';entry.write_text('pass');cfg=tmp_path/'c.json';cfg.write_text(json.dumps({'control_plane_url':'https://control.example','manifest_url':'https://releases.example/m','allowed_hosts':['releases.example'],'hmac_key_file':str(key),'entrypoint_path':str(entry),'poll_seconds':1}))
 events=[]
 class Stop(BaseException):pass
 class S:
  def ensure_current_running(s):events.append('ensure')
 def update():events.append('update')
 def sleep(_):
  events.append('sleep')
  if events.count('sleep')==2:raise Stop()
 with pytest.raises(Stop):rc.main(['--config',str(cfg),'--watch'],supervisor_factory=lambda:S(),update=update,sleeper=sleep)
 assert events==['ensure','update','sleep','ensure','update','sleep']
def test_candidate_delayed_stop_waits_before_relaunching_old(tmp_path):
 # The production helper polls until exit; delayed fake proves no immediate relaunch.
 class Delayed:
  def __init__(s):s.n=0;s.terminated=False
  def poll(s):s.n+=1;return None if s.n<4 else 1
  def terminate(s):s.terminated=True
 s,t=sup(tmp_path,[P()]);d=Delayed();assert s.stop_process(d,20) and d.terminated and d.n>=4
def test_bootstrap_extract_installer_layout_can_start_000(tmp_path):
 import shutil
 archive=Path(__file__).parents[2]/'release-artifacts/node-release-channel-bootstrap-v1.zip';out=tmp_path/'out'
 with zipfile.ZipFile(archive) as z:z.extractall(out)
 root=tmp_path/'root';shutil.copytree(out/'gpunode'/'bootstrap',root);entry=tmp_path/'entry';entry.write_text('pass');token=tmp_path/'token';token.write_text('x');calls=[]
 state=rc.StateClient('https://control',token,lambda *a:{}) ;s=rc.ReleaseSupervisor(root,entry,state,popen=lambda *a,**k:calls.append(a) or P())
 s.ensure_current_running();assert rc.pointer(root/'current.json')['current']['version']=='0.0.0' and (root/'releases'/'0.0.0').is_dir() and len(calls)==1 and '0.0.0/gpunode' in calls[0][0][2]
def test_installer_cleanup_block_is_commit_only():
 text=(Path(__file__).parents[1]/'scripts'/'install_release_channel.ps1').read_text();main,cleanup=text.split("try { if($committed",1)
 assert "$committed=$true } catch" in main and "if(-not $committed){if($activatedNew" in main and "Remove-Item -LiteralPath $InstallRoot" in main
 assert '$InstallRoot' not in cleanup.split('catch { }',1)[0] and 'schtasks' not in cleanup.lower() and 'Move-Item -LiteralPath $backup' not in cleanup
def test_real_supervisor_main_watch_restarts_only_dead_child(tmp_path):
 key=tmp_path/'key';key.write_text('x');entry=tmp_path/'entry';entry.write_text('pass');root=tmp_path/'root';(root/'releases'/'0.0.0').mkdir(parents=True);(root/'current.json').write_text(json.dumps({'current':{'version':'0.0.0','digest':'0'*64},'previous':None}));token=tmp_path/'token';token.write_text('x');events=[];procs=[]
 s=rc.ReleaseSupervisor(root,entry,rc.StateClient('https://c',token,lambda *a:{},clock=lambda:0,sleeper=lambda x:None),popen=lambda *a,**k:procs.append(P()) or procs[-1])
 cfg=tmp_path/'c';cfg.write_text(json.dumps({'control_plane_url':'https://c','manifest_url':'https://releases.example/m','allowed_hosts':['releases.example'],'hmac_key_file':str(key),'entrypoint_path':str(entry),'poll_seconds':1}))
 class Stop(BaseException):pass
 def update():events.append('update')
 def sleep(_):
  events.append('sleep')
  if events.count('sleep')==2:procs[0].dead=True
  if events.count('sleep')==3:raise Stop()
 with pytest.raises(Stop):rc.main(['--config',str(cfg),'--watch'],supervisor_factory=lambda:s,update=update,sleeper=sleep)
 assert len(procs)==2 and events==['update','sleep','update','sleep','update','sleep']
def test_real_update_state_failure_waits_candidate_before_old_relaunch(tmp_path):
 token=tmp_path/'token';token.write_text('x');entry=tmp_path/'entry';entry.write_text('pass');root=tmp_path/'r';(root/'releases'/'1.0.0').mkdir(parents=True);(root/'releases'/'1.0.0'/'.release-meta.json').write_text(json.dumps({'digest':'a'*64}));
 calls=[]
 class Candidate(P):
  def __init__(s):super().__init__();s.n=0
  def poll(s):s.n+=1;return None if s.n<4 else 1
  def terminate(s):s.terminated=True
 class X:
  def __init__(s):s.failed=False;s.posts=[]
  def __call__(s,method,url,h,payload):
   if method=='GET':return {'ready_to_switch':True,'running_pipeline_tasks':0,'running_node_jobs':0}
   s.posts.append(payload)
   if payload['version']=='1.2.3' and payload['ready'] and not s.failed:s.failed=True;raise rc.ReleaseError('x')
   return {}
 x=X();s=rc.ReleaseSupervisor(root,entry,rc.StateClient('https://c',token,x,clock=lambda:0,sleeper=lambda x:None),popen=lambda *a,**k:calls.append(P() if calls else Candidate()) or calls[-1],clock=lambda:0,sleeper=lambda x:None);s.write_pointer({'version':'1.0.0','digest':'a'*64},None);s.child=P(True);p,m=pkg()
 with pytest.raises(rc.ReleaseError):s.update(m,p,KEY,HOSTS,health_seconds=0)
 assert len(calls)==2 and rc.pointer(s.path)['current']['version']=='1.0.0' and x.posts[-1]['version']=='1.0.0' and x.posts[-1]['ready']
