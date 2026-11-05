"""Runs one pipeline script and reports how it ended.

Every stage goes through a Runner, so where a stage executes can change without touching the pipeline:
  LocalRunner   a plain subprocess on this machine (default).
  DockerRunner  a throwaway container with no API keys, CPU/memory/process limits and no access to the
                rest of the machine except the job folder. Used for the stages in FINANALYST_SANDBOX_STAGES.
"""
import os
import signal
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config
from .env import sandbox_env


@dataclass
class CommandSpec:
    stage: str
    script: Path
    args: list
    cwd: Path
    env: dict
    timeout: int
    job_dir: Path
    extra: dict = field(default_factory=dict)


@dataclass
class RunResult:
    returncode: int
    timed_out: bool = False
    cancelled: bool = False
    duration: float = 0.0
    log_tail: str = ""


def tail_of(path: Path, nbytes: int = 6000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _exec(cmd: list, cwd: Path, env: dict, log_path: Path, timeout: int, cancel: threading.Event,
          header: str, on_kill=None) -> RunResult:
    """Run cmd with output going to log_path. Never waits on stdin, so an interactive prompt cannot hang a job."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = cancelled = False
    with open(log_path, "ab") as log:
        log.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  {header}\n".encode("utf-8"))
        log.flush()
        kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                  else {"start_new_session": True})
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, **kwargs)
        except OSError as e:
            log.write(f"cannot start the process: {e}\n".encode("utf-8"))
            return RunResult(127, duration=time.monotonic() - started, log_tail=f"cannot start the process: {e}")
        while True:
            try:
                rc = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel.is_set():
                cancelled = True
            elif time.monotonic() - started > timeout:
                timed_out = True
            if cancelled or timed_out:
                if on_kill:
                    try:
                        on_kill()
                    except Exception:
                        pass
                _kill_tree(proc)
                try:
                    rc = proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    rc = -9
                break
    return RunResult(rc, timed_out, cancelled, time.monotonic() - started, tail_of(log_path))


class Runner(ABC):
    @abstractmethod
    def run(self, spec: CommandSpec, log_path: Path, cancel: threading.Event) -> RunResult:
        ...


class LocalRunner(Runner):
    def run(self, spec, log_path, cancel):
        cmd = [config.PYTHON, "-u", str(spec.script), *spec.args]
        return _exec(cmd, spec.cwd, spec.env, log_path, spec.timeout, cancel, header=f"{spec.stage}: {spec.script.name} {' '.join(spec.args)}")


class DockerRunner(Runner):
    """Runs the script inside a container. The code folder is mounted read-only, the job folder read-write.
    The container gets no API keys (only the variables from env.sandbox_env)."""

    @staticmethod
    def _to_container(arg: str, job_dir: Path, code_dir: Path) -> str:
        try:
            p = Path(arg)
            if not p.is_absolute():
                return arg
            for host, target in ((job_dir, "/work"), (code_dir, "/app")):
                try:
                    rel = p.relative_to(host)
                except ValueError:
                    continue
                return target + (("/" + rel.as_posix()) if rel.parts else "")
        except (ValueError, OSError):
            pass
        return arg

    def run(self, spec, log_path, cancel):
        name = f"finanalyst-{spec.stage}-{uuid.uuid4().hex[:8]}"
        code_dir = spec.script.parent
        args = []
        for a in spec.args:
            if "=" in a and a.startswith("--"):
                k, _, v = a.partition("=")
                args.append(f"{k}={self._to_container(v, spec.job_dir, code_dir)}")
            else:
                args.append(self._to_container(a, spec.job_dir, code_dir))
        cmd = ["docker", "run", "--rm", "--name", name, "--init", "--shm-size=1g",
               "--memory", config.DOCKER_MEMORY, "--cpus", config.DOCKER_CPUS, "--pids-limit", config.DOCKER_PIDS,
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--tmpfs", "/tmp:rw,size=512m",
               "-v", f"{spec.job_dir}:/work", "-v", f"{code_dir}:/app:ro", "-w", "/app"]
        if os.name != "nt" and hasattr(os, "getuid"):
            cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
        for k, v in sandbox_env().items():
            cmd += ["-e", f"{k}={v}"]
        cmd += config.DOCKER_EXTRA_ARGS
        cmd += [config.DOCKER_IMAGE, "python", "-u", f"/app/{spec.script.name}", *args]

        def kill_container():
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)

        return _exec(cmd, spec.cwd, dict(os.environ), log_path, spec.timeout, cancel,
                     header=f"{spec.stage} (docker {name}): {spec.script.name} {' '.join(args)}", on_kill=kill_container)


_local = LocalRunner()
_docker = DockerRunner()


def get_runner(stage: str) -> Runner:
    base = stage.split("_")[0] if stage.startswith("rca_") else stage
    if config.SANDBOX == "docker" and base in config.SANDBOX_STAGES:
        return _docker
    return _local
