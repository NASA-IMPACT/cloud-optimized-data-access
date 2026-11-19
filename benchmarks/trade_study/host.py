"""Where a trial ran and on what code: the provenance every row carries."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import psutil

REPO_ROOT = Path(__file__).resolve().parents[2]
# Versions recorded in every row.
PACKAGES = (
    "earthaccess",
    "earthaccess-auth",
    "icechunk",
    "xarray",
    "zarr",
    "h5py",
    "h5netcdf",
    "s3fs",
    "obstore",
    "obspec-utils",
    "virtualizarr",
    "numpy",
    "dask",
)
IMDS = "http://169.254.169.254/latest"


def provenance() -> dict[str, Any]:
    """The commit, the lock file's hash and the versions of the libraries under test."""
    try:
        git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        sha = git.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        sha = None
    lock = REPO_ROOT / "uv.lock"
    packages = {}
    for name in PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {
        "git_sha": sha,
        "uv_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        "packages": packages,
    }


def _proc_value(path: str, key: str) -> str | None:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _imds(path: str, timeout: float = 0.5) -> str | None:
    """One value from the EC2 instance metadata service; None anywhere else."""
    try:
        token_request = urllib.request.Request(
            f"{IMDS}/api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}
        )
        token = urllib.request.urlopen(token_request, timeout=timeout).read().decode()
        request = urllib.request.Request(f"{IMDS}/meta-data/{path}", headers={"X-aws-ec2-metadata-token": token})
        return urllib.request.urlopen(request, timeout=timeout).read().decode()
    except Exception:  # noqa: BLE001 -- off EC2 or IMDS disabled; the field is just null
        return None


def host_info(imds: bool = True) -> dict[str, Any]:
    """CPU, memory and, on EC2, the instance type and zone. ``host_kind`` is
    ``ec2``, ``jupyterhub`` (a shared node), ``codebuild`` or ``other``."""
    memory = _proc_value("/proc/meminfo", "MemTotal")
    info: dict[str, Any] = {
        # /proc on Linux; platform and psutil elsewhere (macOS has no /proc)
        "cpu_model": _proc_value("/proc/cpuinfo", "model name") or platform.processor() or platform.machine() or None,
        "nproc": os.cpu_count(),
        "mem_total_bytes": int(memory.split()[0]) * 1024 if memory else int(psutil.virtual_memory().total),
        "instance_type": None,
        "instance_type_reason": None,
        "availability_zone": None,
        "host_kind": "other",
    }
    if os.environ.get("CODEBUILD_BUILD_ID"):
        info["host_kind"] = "codebuild"
        info["instance_type_reason"] = "codebuild: no IMDS"
        return info
    instance_type = _imds("instance-type") if imds else None
    if instance_type:
        info.update(
            instance_type=instance_type, availability_zone=_imds("placement/availability-zone"), host_kind="ec2"
        )
    else:
        info["instance_type_reason"] = "imds unreachable"
    if os.environ.get("JUPYTERHUB_USER"):
        # a shared node: load and steal are node-wide; network counters are the pod's own
        info["host_kind"] = "jupyterhub"
    return info


def client_region(host: dict[str, Any]) -> str | None:
    """The AWS region this client runs in: BENCH_CLIENT_REGION, else the EC2 zone, else unknown."""
    zone = host.get("availability_zone")
    return os.environ.get("BENCH_CLIENT_REGION") or (zone[:-1] if zone else None)
