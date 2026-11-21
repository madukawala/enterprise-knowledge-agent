"""The sandbox container's own walls, checked inside the running container.

The other sandbox tests run the same code on the test machine. These check
what only the container adds: no network, a read-only file system, a
non-root user, no keys in the environment, no project code in the image,
and the memory limit (enforced on Linux only).

Needs the stack running (`make up`). Run with `make test-sandbox`.
Skipped when the sandbox container is not running.
"""

import json
import shutil
import subprocess
from typing import Any

import pytest


def _container_running() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        state = subprocess.run(
            ["docker", "compose", "ps", "--status", "running", "--services"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "sandbox" in state.stdout.split()


pytestmark = pytest.mark.skipif(not _container_running(), reason="the sandbox is not running")


def _inside(script: str) -> subprocess.CompletedProcess[str]:
    """Run a Python script inside the sandbox container, as its own user."""
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "sandbox", "python", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _post(code: str, mode: str = "analysis", data: Any = None) -> dict[str, Any]:
    """Send code to the sandbox server from inside its container."""
    body = json.dumps({"code": code, "mode": mode, "data": data})
    script = (
        "import json, urllib.request\n"
        f"req = urllib.request.Request('http://127.0.0.1:8100/run', data={body!r}.encode(),"
        " headers={'Content-Type': 'application/json'})\n"
        "print(urllib.request.urlopen(req, timeout=30).read().decode())\n"
    )
    done = _inside(script)
    assert done.returncode == 0, done.stderr
    reply: dict[str, Any] = json.loads(done.stdout)
    return reply


def test_the_container_has_no_network_access() -> None:
    done = _inside(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "    print('connected')\n"
        "except OSError as error:\n"
        "    print('blocked', type(error).__name__)\n"
    )
    assert done.stdout.startswith("blocked")


def test_the_file_system_is_read_only_outside_tmp() -> None:
    done = _inside(
        "for path in ('/app/sandbox/x.py', '/etc/x', '/x'):\n"
        "    try:\n"
        "        open(path, 'w').write('x')\n"
        "        print('wrote', path)\n"
        "    except OSError:\n"
        "        print('refused', path)\n"
    )
    assert "wrote" not in done.stdout
    assert done.stdout.count("refused") == 3


def test_it_runs_as_an_unprivileged_user_with_no_keys() -> None:
    done = _inside(
        "import json, os\nprint(json.dumps({'uid': os.getuid(), 'env': sorted(os.environ)}))\n"
    )
    info = json.loads(done.stdout)
    assert info["uid"] == 10002
    # GPG_KEY is set by the official Python image: the public fingerprint of
    # the key Python releases are signed with, not a secret.
    secrets = [n for n in info["env"] if ("KEY" in n or "PASSWORD" in n) and n != "GPG_KEY"]
    assert secrets == []


def test_the_image_holds_no_project_code_or_libraries() -> None:
    done = _inside(
        "import importlib.util, os\n"
        "print(sorted(os.listdir('/app/sandbox')))\n"
        "print([m for m in ('api', 'agents', 'core', 'fastapi', 'httpx', 'langchain_core',"
        " 'pinecone') if importlib.util.find_spec(m)])\n"
    )
    files, importable = done.stdout.strip().splitlines()
    assert "server.py" in files
    assert importable == "[]"


def test_code_runs_through_the_server() -> None:
    reply = _post("import statistics\nresult = statistics.mean(data)", data=[2, 4, 9])
    assert reply["ok"]
    assert reply["result"] == 5


def test_an_endless_loop_is_stopped_in_the_container() -> None:
    reply = _post("while True:\n    pass")
    assert reply["status"] in {"timeout", "crashed"}


def test_a_memory_bomb_is_stopped_in_the_container() -> None:
    reply = _post("x = 'a' * (10 ** 10)")
    assert not reply["ok"]
    assert reply["status"] in {"error", "crashed"}


def test_escapes_are_refused_in_the_container() -> None:
    reply = _post("import statistics\nresult = statistics.sys.modules['os'].listdir('/')")
    assert not reply["ok"]
