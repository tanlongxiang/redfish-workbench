"""Local app authentication, one-use browser bootstrap, CSRF, and single service lock."""
import hmac
import os
import secrets
import sys
import threading
import time
from pathlib import Path

class ServiceLock:
    def __init__(self, root):self.path=Path(root)/'service.lock';self.fd=None
    def acquire(self):
        self.fd=os.open(self.path,os.O_CREAT|os.O_RDWR,0o600)
        try:
            if sys.platform=='win32':
                import msvcrt
                if os.fstat(self.fd).st_size==0:os.write(self.fd,b' ')
                os.lseek(self.fd,0,0);msvcrt.locking(self.fd,msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,BlockingIOError):
            os.close(self.fd);self.fd=None;raise RuntimeError('此数据目录已有 Web 服务运行；不允许多实例执行任务') from None
    def release(self):
        if self.fd is not None:os.close(self.fd);self.fd=None

class Sessions:
    def __init__(self, root):
        self.lock=threading.Lock();self.sessions={};self.bootstrap=secrets.token_urlsafe(32)
        self.bootstrap_until=time.time()+300;self.failures={}
        path=Path(root)/'access-key'
        if not path.exists():
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32)+'\n')
        self.access_key=path.read_text().strip()
        os.chmod(path,0o600)
    def login(self, key, bootstrap, client):
        with self.lock:
            recent=[t for t in self.failures.get(client,[]) if time.time()-t<60]
            if len(recent)>=5:raise ValueError('登录尝试过多，请稍后再试')
            expected=self.bootstrap if bootstrap else self.access_key
            valid=bool(expected) and hmac.compare_digest(key.encode('utf-8'),expected.encode('utf-8'))
            if bootstrap and time.time()>self.bootstrap_until:valid=False
            if not valid:
                self.failures[client]=recent+[time.time()];raise ValueError('访问口令错误或已过期')
            if bootstrap:self.bootstrap=''
            token=secrets.token_urlsafe(32)
            session={'id':secrets.token_hex(16),'csrf':secrets.token_urlsafe(32),'expires':time.time()+8*3600}
            self.sessions[token]=session
            return token,dict(session)
    def get(self, token):
        with self.lock:
            session=self.sessions.get(token or '')
            if not session or session['expires']<time.time():return None
            return dict(session)
    def logout(self, token):
        with self.lock:self.sessions.pop(token or '',None)
