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
 assert text.index("Move-Item -LiteralPath $stage -Destination $InstallRoot") < text.index("if($activatedNew -and") < text.index("if($moved)")
def test_installer_commit_cleanup_is_outside_rollback():
 text=(Path(__file__).parents[1]/"scripts"/"install_release_channel.ps1").read_text()
 run=text.index("Invoke-Schtasks @('/Run'"); commit=text.index("$committed=$true"); rollback=text.index("if(-not $committed)"); cleanup=text.rindex("try {\n    if($committed")
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
 text=(Path(__file__).parents[1]/'scripts'/'install_release_channel.ps1').read_text();main,cleanup=text.split("try {\n    if($committed",1)
 assert "$committed=$true" in main and "if(-not $committed)" in main and "Remove-Item -LiteralPath $InstallRoot" in main
 assert '$InstallRoot' not in cleanup.split('catch { }',1)[0] and 'schtasks' not in cleanup.lower() and 'Move-Item -LiteralPath $backup' not in cleanup
@pytest.mark.parametrize('payload,ok', [({'schema_version':1,'manifest_url':'https://releases.example/m','allowed_hosts':['releases.example'],'hmac_key_b64':'VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ='},True), ({'schema_version':1,'manifest_url':'http://releases.example/m','allowed_hosts':['releases.example'],'hmac_key_b64':'VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ='},False)])
def test_release_bootstrap_schema(payload,ok,tmp_path):
 token=tmp_path/'token';token.write_text('resident');s=rc.StateClient('https://c',token,lambda *a:payload)
 if ok: assert s.release_bootstrap()[0]=='https://releases.example/m'
 else:
  with pytest.raises(rc.ReleaseError):s.release_bootstrap()
@pytest.mark.parametrize('mutate',[lambda p:p.update(extra=1),lambda p:p.update(manifest_url=3),lambda p:p.update(manifest_url='https://user@releases.example/m'),lambda p:p.update(manifest_url='https://releases.example/m#x'),lambda p:p.update(allowed_hosts=['RELEASES.EXAMPLE']),lambda p:p.update(allowed_hosts=['x','y']),lambda p:p.update(hmac_key_b64='***')])
def test_release_bootstrap_malformed_is_release_error(mutate,tmp_path):
 p={'schema_version':1,'manifest_url':'https://releases.example/m','allowed_hosts':['releases.example'],'hmac_key_b64':'VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ='};mutate(p);token=tmp_path/'t';token.write_text('resident');s=rc.StateClient('https://c',token,lambda *a:p)
 with pytest.raises(rc.ReleaseError):s.release_bootstrap()
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


def test_main_bootstrap_retries_without_stopping_current_child(tmp_path):
    entry=tmp_path/'entry';entry.write_text('pass');token=tmp_path/'token';token.write_text('resident')
    root=tmp_path/'root';(root/'releases'/'0.0.0').mkdir(parents=True);(root/'current.json').write_text(json.dumps({'current':{'version':'0.0.0','digest':'0'*64},'previous':None}))
    bootstrap={'schema_version':1,'manifest_url':'https://releases.example/m','allowed_hosts':['releases.example'],'hmac_key_b64':'VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ='}
    child=[]; state=rc.StateClient('https://control',token,lambda *a:bootstrap)
    s=rc.ReleaseSupervisor(root,entry,state,popen=lambda *a,**k:child.append(P()) or child[-1])
    attempts=[]
    def update(url,key,hosts):
        attempts.append((url,key,hosts))
        if len(attempts)==2: raise rc.ReleaseError('temporary')
    s.update_from_manifest_url=update
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps({'control_plane_url':'https://control','entrypoint_path':str(entry),'poll_seconds':1}))
    class Stop(BaseException): pass
    def sleep(_):
        if len(attempts)>=3: raise Stop()
    with pytest.raises(Stop): rc.main(['--config',str(cfg),'--watch'],supervisor_factory=lambda:s,sleeper=sleep)
    assert len(child)==1 and child[0].poll() is None and len(attempts)==3


def test_control_transport_rejects_redirect_bounds_body_and_never_reuses_bearer():
    seen=[]
    class R:
        def __init__(s,body,url='https://control.example/api'):s.body=body;s.url=url
        def __enter__(s):return s
        def __exit__(s,*x):pass
        def geturl(s):return s.url
        def read(s,n):
            bit,s.body=s.body[:n],s.body[n:]
            return bit
    def opener(request,timeout):
        seen.append((request.full_url,request.get_header('Authorization')))
        return R(b'{"ok":true}')
    assert rc.urllib_transport('GET','https://control.example/api',{'Authorization':'Bearer resident'},opener=opener)=={'ok':True}
    assert seen==[('https://control.example/api','Bearer resident')]
    with pytest.raises(rc.ReleaseError):
        rc.urllib_transport('GET','https://control.example/api',{'Authorization':'Bearer resident'},opener=lambda *a:R(b'{}','https://evil.example/x'))
    with pytest.raises(rc.ReleaseError):
        rc.urllib_transport('GET','https://control.example/api',{},opener=lambda *a:R(b'x'*(rc.MAX_JSON_BYTES+1)))
    with pytest.raises(rc.ReleaseError):
        rc.urllib_transport('GET','https://control.example/api',{},opener=lambda *a:R(b'not-json'))


def test_main_uses_defined_production_transport_and_bom_config(tmp_path,monkeypatch):
    entry=tmp_path/'entry.py';entry.write_text('pass');token=tmp_path/'token';token.write_text('resident')
    cfg=tmp_path/'config.json';cfg.write_bytes(b'\xef\xbb\xbf'+json.dumps({'control_plane_url':'https://control.example','entrypoint_path':str(entry),'token_file':str(token),'poll_seconds':1}).encode())
    assert rc._config(cfg)['entrypoint_path']==str(entry)
    calls=[]
    def transport(method,url,headers,payload):
        calls.append((method,url,headers));return {'schema_version':1,'manifest_url':'https://releases.example/m','allowed_hosts':['releases.example'],'hmac_key_b64':'VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ='}
    class S:
        def __init__(s,state):s.state=state
        def ensure_current_running(s):pass
        def update_from_manifest_url(s,url,key,hosts):assert (url,hosts)==('https://releases.example/m',{'releases.example'})
    monkeypatch.setattr(rc,'urllib_transport',transport)
    monkeypatch.setattr(rc,'ReleaseSupervisor',lambda root,entry,state,**kwargs:S(state))
    assert rc.main(['--config',str(cfg)])==0
    assert calls[0][0]=='GET' and calls[0][2]['Authorization']=='Bearer resident'


def test_main_one_shot_failure_is_nonzero_watch_retries_without_details(tmp_path,capsys):
    cfg=tmp_path/'c';cfg.write_text(json.dumps({'control_plane_url':'https://control.example','entrypoint_path':'x','poll_seconds':1}))
    class S:
        def ensure_current_running(s):raise rc.ReleaseError('secret-url')
    assert rc.main(['--config',str(cfg)],supervisor_factory=lambda:S())==1
    assert 'secret-url' not in capsys.readouterr().err
    events=[]
    class Stop(BaseException):pass
    def sleep(_):
        events.append('sleep')
        if len(events)==2:raise Stop()
    with pytest.raises(Stop):rc.main(['--config',str(cfg),'--watch'],supervisor_factory=lambda:S(),sleeper=sleep)
    assert events==['sleep','sleep'] and 'secret-url' not in capsys.readouterr().err


def test_installer_writes_no_bom_config_and_document_uses_flat_entrypoint():
    text=(Path(__file__).parents[1]/'scripts'/'install_release_channel.ps1').read_text()
    doc=(Path(__file__).parents[1]/'RELEASE-3060.md').read_text()
    assert 'UTF8Encoding($false)' in text and 'WriteAllText($configPath' in text and 'if($committed){ exit 0 }' in text
    assert "[Alias('PythonwPath')][string]$PythonPath=\"python.exe\"" in text and 'base_python_path=$interpreters.base_executable' in text
    assert "E:\\peiyin-node\\entrypoint.py" in doc and "E:\\peiyin-node\\gpunode\\entrypoint.py" not in doc


def test_windows_bypasses_venv_launcher_and_preserves_venv_environment(tmp_path,monkeypatch):
    class Startup:
        def __init__(s):s.dwFlags=0;s.wShowWindow=0
    class Child:
        def poll(s):return None
    monkeypatch.setattr(rc,'IS_WINDOWS',True);monkeypatch.setattr(rc.subprocess,'STARTUPINFO',Startup,raising=False)
    monkeypatch.setattr(rc.subprocess,'STARTF_USESHOWWINDOW',1,raising=False);monkeypatch.setattr(rc.subprocess,'CREATE_NO_WINDOW',8,raising=False)
    root=tmp_path/'root';(root/'releases'/'1.2.3').mkdir(parents=True);entry=tmp_path/'entry.py';entry.write_text('pass');calls=[]
    s=rc.ReleaseSupervisor(root,entry,None,popen=lambda argv,**kw:calls.append((argv,kw)) or Child(),interpreter='E:/node/.venv/Scripts/python.exe',base_interpreter='C:/Python39/python.exe')
    s.launch('1.2.3')
    assert calls[0][0][0]=='C:/Python39/python.exe'
    assert calls[0][1]['env']['__PYVENV_LAUNCHER__']=='E:/node/.venv/Scripts/python.exe'
    assert calls[0][1]['creationflags']&8 and calls[0][1]['close_fds'] is False


def test_main_routes_interpreter_paths_from_config(tmp_path,monkeypatch):
    entry=tmp_path/'entry';entry.write_text('pass');cfg=tmp_path/'config';cfg.write_text(json.dumps({'control_plane_url':'https://control.example','entrypoint_path':str(entry),'entrypoint_python_path':'E:/venv/python.exe','base_python_path':'C:/Python/python.exe'}))
    seen={}
    class S:
        def ensure_current_running(s):pass
        class state:
            @staticmethod
            def release_bootstrap():return 'https://releases.example/m',{'releases.example'},b'k'*32
        def update_from_manifest_url(s,*a):pass
    def factory(root,entry,state,**kwargs):seen.update(kwargs);return S()
    monkeypatch.setattr(rc,'ReleaseSupervisor',factory)
    assert rc.main(['--config',str(cfg)])==0
    assert seen=={'interpreter':'E:/venv/python.exe','base_interpreter':'C:/Python/python.exe'}


def test_bootstrap_v2_is_immutable_and_matches_final_sources():
    root=Path(__file__).parents[2]; artifacts=root/'release-artifacts'
    v1=artifacts/'node-release-channel-bootstrap-v1.zip';v2=artifacts/'node-release-channel-bootstrap-v2.zip'
    assert hashlib.sha256(v1.read_bytes()).hexdigest()=='ea12af690d6b824fbb1fc8305d45f400c0ad452be9545ab2bb9eb554242cf7b3'
    actual=hashlib.sha256(v2.read_bytes()).hexdigest()
    assert (artifacts/'node-release-channel-bootstrap-v2.sha256').read_text().strip()==actual+'  node-release-channel-bootstrap-v2.zip'
    expected={'gpunode/bootstrap/current.json','gpunode/bootstrap/releases/0.0.0/.release-meta.json','gpunode/release_channel.py','gpunode/scripts/install_release_channel.ps1','gpunode/RELEASE-3060.md'}
    with zipfile.ZipFile(v2) as archive:
        assert set(archive.namelist())==expected
        for name in ('gpunode/release_channel.py','gpunode/scripts/install_release_channel.ps1','gpunode/RELEASE-3060.md'):
            assert archive.read(name)==(root/name).read_bytes()
