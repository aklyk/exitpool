"""One temporary Happ imports the subscription; its state seeds the first worker."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from configuration import validate_catalog
from memory_budget import read_memory, parse_docker_mib
from image_build import IMAGE, prepare_image


class Discovery:
    def __init__(self, source, subscription, timeout=300, workdir=None):
        self.source, self.subscription, self.timeout = Path(source), subscription, timeout
        self.workdir = workdir
        self.name = 'exitpool-discovery-'+secrets.token_hex(6)
        self.temporary = None
        self.started = False
        self.state = None

    def __enter__(self):
        try:
            prepare_image(self.source)
            # Docker bind-mounts this path, so it must be visible in the host namespace.
            self.temporary = tempfile.TemporaryDirectory(prefix='exitpool-discovery-', dir=self.workdir)
            base = Path(self.temporary.name)
            self.state = base/'state'; self.state.mkdir(mode=0o700)
            key = base/'subscription.secret'
            fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(self.subscription+'\n')
            self.started = True # cleanup also covers interrupted/partially successful docker run
            subprocess.run(['docker','run','-d','--name',self.name,
                '--label','app=exitpool-discovery','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--read-only','--pids-limit','256',
                '--memory','384m','--memory-swap','512m',
                '--tmpfs','/tmp:rw,nosuid,nodev,size=64m','--tmpfs','/run:rw,nosuid,nodev,size=32m',
                '--mount',f'type=bind,src={self.state},dst=/state',
                '--mount',f'type=bind,src={key},dst=/run/secrets/subscription,readonly',
                IMAGE,'bash','/app/entrypoint.sh','--discover'],check=True,stdout=subprocess.DEVNULL)
            print('Happ импортирует подписку и читает названия профилей…',flush=True)
            deadline=time.monotonic()+self.timeout
            while time.monotonic()<deadline:
                path=self.state/'catalog.json'
                if path.exists():
                    self.catalog=validate_catalog(json.loads(path.read_text()))
                    return self
                try:
                    status=json.loads((self.state/'status.json').read_text())
                    if status.get('phase')=='failed':
                        raise ValueError('Happ не смог прочитать подписку: '+status.get('error','ошибка клиента'))
                except (FileNotFoundError,json.JSONDecodeError):pass
                r=subprocess.run(['docker','inspect','--format','{{.State.Running}}',self.name],
                                 capture_output=True,text=True,timeout=10)
                if r.returncode or r.stdout.strip()!='true':
                    raise ValueError('Первый Happ остановился до получения списка профилей.')
                time.sleep(2)
            raise ValueError('Не удалось получить список профилей за 5 минут. Проверьте доступность подписки.')
        except BaseException:
            self.__exit__(None,None,None)
            raise

    def stop(self):
        if self.started:
            subprocess.run(['docker','stop','-t','15',self.name],check=True,
                           stdout=subprocess.DEVNULL,timeout=25)
            subprocess.run(['docker','rm',self.name],check=True,
                           stdout=subprocess.DEVNULL,timeout=15)
            self.started=False

    def memory(self):
        r=subprocess.run(['docker','stats','--no-stream','--format','{{.MemUsage}}',self.name],
                         capture_output=True,text=True,timeout=12)
        first=parse_docker_mib(r.stdout) if r.returncode==0 else 0
        return read_memory(first_mib=first)

    def __exit__(self,*args):
        if self.started:
            # First try graceful shutdown, then force-remove only our unique container.
            try:self.stop()
            except (subprocess.SubprocessError,OSError):
                r=subprocess.run(['docker','rm','-f',self.name],capture_output=True,timeout=20)
                if r.returncode:
                    print('Не удалось убрать временный контейнер '+self.name+'; его приватные файлы сохранены.')
                    # Detach cleanup: never remove the secret/state from a running container.
                    if self.temporary:self.temporary._finalizer.detach()
                    return
        if self.temporary:self.temporary.cleanup()
