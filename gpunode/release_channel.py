"""Stable stdlib-only release supervisor; importing it has no side effects."""
from __future__ import annotations
import hashlib,hmac,io,json,os,re,shutil,stat,subprocess,sys,tempfile,time,urllib.parse,urllib.request,uuid,zipfile
from pathlib import Path,PurePosixPath

VERSION=re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$"); SHA=re.compile(r"^[0-9a-f]{64}$")
MAX_PACKAGE_BYTES=256*1024*1024; IS_WINDOWS=os.name=="nt"
class ReleaseError(RuntimeError): pass
def digest(data): return hashlib.sha256(data).hexdigest()
def under(child,parent):
    try: child.resolve().relative_to(parent.resolve()); return True
    except ValueError: return False
def canonical_manifest(m): return json.dumps({k:v for k,v in m.items() if k!="signature"},sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
def validate_url(url,hosts):
    p=urllib.parse.urlsplit(url)
    if p.scheme!="https" or not p.hostname or p.hostname.lower() not in hosts or p.username or p.password: raise ReleaseError("untrusted release URL")
    return url
def safe_member_name(raw):
    if not raw or "\\" in raw or ":" in raw or raw.startswith(("/","\\")): raise ReleaseError("unsafe package path")
    p=PurePosixPath(raw)
    if p.is_absolute() or any(x in ("",".","..") for x in p.parts): raise ReleaseError("unsafe package path")
    low=[x.lower() for x in p.parts]
    if any(x in {"entrypoint.py","workdir","models"} or any(y in x for y in ("token","secret","key")) for x in low): raise ReleaseError("reserved package path")
    return p
def validate_manifest(m,key,hosts):
    if set(m)!={"version","source_revision","package_url","package_sha256","files","signature"} or not VERSION.fullmatch(str(m.get("version"))) or not isinstance(m.get("source_revision"),str) or not m["source_revision"] or not key: raise ReleaseError("invalid manifest")
    if not SHA.fullmatch(str(m.get("package_sha256"))) or not isinstance(m.get("files"),dict) or not m["files"]: raise ReleaseError("invalid manifest")
    names=set()
    for n,d in m["files"].items():
        n=str(safe_member_name(str(n)))
        if n in names or not SHA.fullmatch(str(d)): raise ReleaseError("invalid manifest")
        names.add(n)
    validate_url(m["package_url"],hosts)
    if not hmac.compare_digest(hmac.new(key,canonical_manifest(m),hashlib.sha256).hexdigest(),str(m["signature"])): raise ReleaseError("manifest signature rejected")
class AllowlistedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self,hosts): super().__init__(); self.hosts=hosts
    def redirect_request(self,req,fp,code,msg,headers,newurl): validate_url(newurl,self.hosts); return super().redirect_request(req,fp,code,msg,headers,newurl)
def _open(request,hosts): return urllib.request.build_opener(AllowlistedRedirect(hosts)).open(request,timeout=30)
def download_limited(url,hosts,opener=None,max_bytes=MAX_PACKAGE_BYTES):
    validate_url(url,hosts)
    try:
        response=(opener or (lambda r,timeout:_open(r,hosts)))(urllib.request.Request(url),timeout=30)
        with response:
            validate_url(getattr(response,"geturl",lambda:url)(),hosts); out=[]; total=0
            while True:
                bit=response.read(min(65536,max_bytes+1-total))
                if not bit: break
                total+=len(bit)
                if total>max_bytes: raise ReleaseError("package exceeds size limit")
                out.append(bit)
            return b"".join(out)
    except ReleaseError: raise
    except Exception as e: raise ReleaseError(e.__class__.__name__) from None
def download_manifest(url,hosts,opener=None):
    try: value=json.loads(download_limited(url,hosts,opener,1024*1024).decode())
    except (UnicodeDecodeError,json.JSONDecodeError) as e: raise ReleaseError("invalid manifest") from e
    if not isinstance(value,dict): raise ReleaseError("invalid manifest")
    return value
def special(info): return ((info.external_attr>>16)&0o170000) not in (0,stat.S_IFREG,stat.S_IFDIR)
def validate_package(package,m,destination):
    if len(package)>MAX_PACKAGE_BYTES or digest(package)!=m["package_sha256"]: raise ReleaseError("package integrity rejected")
    expected={str(safe_member_name(str(n))):d for n,d in m["files"].items()}
    try:
        with zipfile.ZipFile(io.BytesIO(package)) as z:
            files=[]; seen=set(); declared=0
            for info in z.infolist():
                raw=info.filename.rstrip("/")
                if not raw: continue
                name=str(safe_member_name(raw))
                if name in seen or special(info): raise ReleaseError("unsafe package member")
                seen.add(name)
                if info.is_dir(): continue
                if name not in expected: raise ReleaseError("unexpected package member")
                declared+=info.file_size
                if declared>MAX_PACKAGE_BYTES: raise ReleaseError("package expansion exceeds size limit")
                files.append((info,name))
            if {n for _,n in files}!=set(expected): raise ReleaseError("package file list mismatch")
            destination.mkdir(parents=True,exist_ok=False)
            if not under(destination,destination.parent): raise ReleaseError("unsafe destination")
            actual=0
            for info,name in files:
                body=z.read(info); actual+=len(body)
                if actual>MAX_PACKAGE_BYTES or digest(body)!=expected[name]: raise ReleaseError("file integrity rejected")
                output=destination.joinpath(*PurePosixPath(name).parts); output.parent.mkdir(parents=True,exist_ok=True)
                if not under(output,destination): raise ReleaseError("unsafe destination")
                output.write_bytes(body)
    except (OSError,zipfile.BadZipFile,ReleaseError): shutil.rmtree(destination,ignore_errors=True); raise
class StateClient:
    def __init__(self,url,token_file,transport,clock=time.monotonic,sleeper=time.sleep): self.url=url.rstrip("/"); self.token_file=token_file; self.transport=transport; self.clock=clock; self.sleeper=sleeper
    def request(self,method,path,payload=None):
        try: token=self.token_file.read_text().strip()
        except OSError: token=""
        if not token: raise ReleaseError("resident token unavailable")
        try: return self.transport(method,self.url+path,{"Authorization":"Bearer "+token},payload)
        except Exception as e: raise ReleaseError(e.__class__.__name__) from None
    def report(self,v,d,ready,draining): self.request("POST","/api/nodes/me/release-state",{"version":v,"digest":d,"ready":ready,"draining":draining})
    def wait_ready(self,timeout):
        end=self.clock()+timeout
        while True:
            r=self.request("GET","/api/nodes/me/release-switch-ready"); r=r if isinstance(r,dict) else r.json()
            if r.get("ready_to_switch") and not r.get("running_pipeline_tasks",0) and not r.get("running_node_jobs",0): return True
            if self.clock()>=end: return False
            self.sleeper(2)
def pointer(path):
    try: p=json.loads(path.read_text())
    except FileNotFoundError: return {"current":None,"previous":None}
    if not isinstance(p,dict) or set(p)!={"current","previous"}: raise ReleaseError("invalid release pointer")
    return p
class ReleaseSupervisor:
    def __init__(self,root,entrypoint,state,popen=subprocess.Popen,clock=time.monotonic,sleeper=time.sleep): self.root=Path(root); self.entrypoint=Path(entrypoint); self.state=state; self.popen=popen; self.clock=clock; self.sleeper=sleeper; self.releases=self.root/"releases"; self.path=self.root/"current.json"; self.child=None
    def write_pointer(self,current,previous):
        self.root.mkdir(parents=True,exist_ok=True); tmp=self.root/(".pointer-"+uuid.uuid4().hex); tmp.write_text(json.dumps({"current":current,"previous":previous},sort_keys=True)); os.replace(tmp,self.path)
    def install(self,m,package):
        self.releases.mkdir(parents=True,exist_ok=True); version=m["version"]; d=m["package_sha256"]; target=self.releases/version
        meta=target/".release-meta.json"
        if target.exists():
            try:
                if json.loads(meta.read_text())=={"digest":d}: return {"version":version,"digest":d}
            except Exception: pass
            raise ReleaseError("immutable version already exists")
        stage=Path(tempfile.mkdtemp(prefix=".release-",dir=self.releases)); content=stage/"content"
        try:
            validate_package(package,m,content); (content/".release-meta.json").write_text(json.dumps({"digest":d})); os.replace(content,target); stage.rmdir()
        except Exception: shutil.rmtree(stage,ignore_errors=True); raise
        return {"version":version,"digest":d}
    def launch(self,version):
        release=self.releases/version
        if not release.is_dir() or not self.entrypoint.is_file(): raise ReleaseError("release launch target unavailable")
        py=Path(sys.executable).with_name("pythonw.exe") if IS_WINDOWS else Path(sys.executable); code="import runpy,sys;sys.path[:0]="+repr([str(release/"gpunode"),str(self.entrypoint.parent)])+";runpy.run_path("+repr(str(self.entrypoint))+",run_name='__main__')"; kw={"close_fds":not IS_WINDOWS}
        if IS_WINDOWS:
            kw["creationflags"]=getattr(subprocess,"CREATE_NO_WINDOW",0x08000000); s=subprocess.STARTUPINFO(); s.dwFlags|=subprocess.STARTF_USESHOWWINDOW; s.wShowWindow=0; kw["startupinfo"]=s
        self.child=self.popen([str(py),"-c",code],**kw); return self.child
    def ensure_current_running(self):
        cur=pointer(self.path)["current"]
        if cur and (self.child is None or self.child.poll() is not None): return self.launch(cur["version"])
        return self.child
    def stop_child(self,timeout=20):
        old=self.child
        if old is None or old.poll() is not None: return
        old.terminate(); end=self.clock()+timeout; spins=0
        while old.poll() is None and self.clock()<end and spins<100: self.sleeper(.5); spins+=1
        if old.poll() is None: raise ReleaseError("old child did not stop")
        self.child=None
    def stop_process(self,process,timeout=20):
        if process is None or process.poll() is not None:return True
        process.terminate(); end=self.clock()+timeout; spins=0
        while process.poll() is None and self.clock()<end and spins<100:self.sleeper(.5);spins+=1
        return process.poll() is not None
    def healthy(self,child,seconds):
        end=self.clock()+seconds
        while self.clock()<end:
            if child.poll() is not None:return False
            self.sleeper(min(1,max(0,end-self.clock())))
        return child.poll() is None
    def restore(self,old,previous):
        if old: self.write_pointer(old,previous); self.launch(old["version"]); self.state.report(old["version"],old["digest"],True,False)
        else: self.write_pointer(None,None); self.state.report("","",False,False)
    def update(self,m,package,key,hosts,drain_timeout=300,health_seconds=15):
        validate_manifest(m,key,hosts); new=self.install(m,package); before=pointer(self.path); old,previous=before["current"],before["previous"]
        if old==new: self.ensure_current_running(); self.state.report(old["version"],old["digest"],True,False); return True
        v,d=((old or {}).get("version",""),(old or {}).get("digest","")); self.state.report(v,d,False,True); switched=False; candidate=None
        try:
            if not self.state.wait_ready(drain_timeout): self.state.report(v,d,bool(old),False); return False
            self.stop_child(); self.write_pointer(new,old); switched=True; candidate=self.launch(new["version"])
            if not self.healthy(candidate,health_seconds): raise ReleaseError("new release health failed")
            self.state.report(new["version"],new["digest"],True,False); return True
        except Exception:
            if not switched:
                self.state.report(v,d,bool(old),False)
                raise ReleaseError("release switch aborted") from None
            if candidate is not None and not self.stop_process(candidate):
                self.child=candidate
                raise ReleaseError("candidate did not stop") from None
            self.restore(old,previous); raise ReleaseError("release reverted") from None
    def update_from_manifest_url(self,url,key,hosts,opener=None):
        m=download_manifest(url,hosts,opener); validate_manifest(m,key,hosts); return self.update(m,download_limited(m["package_url"],hosts,opener),key,hosts)
def _config(path):
    c=json.loads(Path(path).read_text(encoding="utf-8")); hosts={str(x).lower() for x in c.get("allowed_hosts",[])}
    if not hosts or not c.get("hmac_key_file") or not c.get("entrypoint_path") or int(c.get("poll_seconds",900))<1: raise ReleaseError("invalid release configuration")
    if urllib.parse.urlsplit(c.get("control_plane_url","")).scheme!="https": raise ReleaseError("invalid release configuration")
    validate_url(c.get("manifest_url",""),hosts)
    return c,hosts
def main(argv=None, supervisor_factory=None, update=None, sleeper=time.sleep):
    import argparse
    p=argparse.ArgumentParser();p.add_argument("--config",required=True);p.add_argument("--watch",action="store_true");a=p.parse_args(argv)
    try:
        c,hosts=_config(a.config); entry=Path(c["entrypoint_path"]); key=Path(c["hmac_key_file"]).read_bytes().strip()
        if not key: raise ReleaseError("invalid release configuration")
        sf=supervisor_factory or (lambda: ReleaseSupervisor(Path(a.config).parent,entry,StateClient(c["control_plane_url"],Path(c.get("token_file",entry.parent/"workdir"/"node_token.txt")),urllib_transport)))
        s=sf()
        while True:
            s.ensure_current_running()
            try: (update or (lambda:s.update_from_manifest_url(c["manifest_url"],key,hosts)))()
            except ReleaseError: pass
            if not a.watch:return 0
            sleeper(int(c.get("poll_seconds",900)))
    except (ReleaseError,OSError,json.JSONDecodeError,ValueError) as e:
        print("release channel configuration failed",file=sys.stderr);return 2
if __name__=="__main__": sys.exit(main())
