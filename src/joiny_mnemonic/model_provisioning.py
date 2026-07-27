"""Provision the local extractor runtime and model as part of installation.

The user picks a model during setup; everything else — fetching a llama.cpp
server build, fetching the weights, verifying both against pinned hashes and
writing the backend block — happens here. No separate "now go install a model"
step, no manually started server.

Every artifact is pinned by sha256, and the pinned digest is what the backend
descriptor reports as the model revision. That is deliberate: the evaluation
gate (JM-INV-007) hashes the revision, so a silently swapped file cannot keep
an old signed report valid.

Provisioning is pinned for win-x64 only, because that is the platform where it
has actually been exercised. Elsewhere it refuses with a message pointing at
the manual backend block rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .extractor_backend import BackendConfig


class ProvisioningError(RuntimeError):
    """A provisioning step failed loudly instead of half-installing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    title: str
    url: str
    filename: str
    sha256: str
    size_bytes: int
    context_tokens: int
    notes: str

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / 1_000_000_000, 2)


#: Stage 6 candidates, pinned by content hash rather than by tag.
MODEL_CATALOG: dict[str, ModelSpec] = {
    "qwen3-4b": ModelSpec(
        key="qwen3-4b",
        title="Qwen3 4B Instruct 2507 (Q4_K_M)",
        url=(
            "https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/"
            "resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
        ),
        filename="Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        # Content sha256 (the LFS oid). HuggingFace's ETag is an Xet hash and
        # is NOT the file digest — pinning that would refuse every download.
        sha256="3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
        size_bytes=2_497_281_120,
        context_tokens=8192,
        notes="strong multilingual instruction following; default candidate",
    ),
    "gemma-3-4b": ModelSpec(
        key="gemma-3-4b",
        title="Gemma 3 4B Instruct (Q4_K_M)",
        url=(
            "https://huggingface.co/ggml-org/gemma-3-4b-it-GGUF/"
            "resolve/main/gemma-3-4b-it-Q4_K_M.gguf"
        ),
        filename="gemma-3-4b-it-Q4_K_M.gguf",
        sha256="882e8d2db44dc554fb0ea5077cb7e4bc49e7342a1f0da57901c0802ea21a0863",
        size_bytes=2_489_757_856,
        context_tokens=8192,
        notes="second candidate; comparison partner for the swap recipe",
    ),
}

DEFAULT_MODEL = "qwen3-4b"

RUNTIME_RELEASE = "b10141"
_RELEASE_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/" + RUNTIME_RELEASE
)

#: Preferred build first. The Vulkan build offloads to any modern GPU and still
#: runs when there is none; the CPU build is the fallback when the Vulkan
#: binary cannot load (no ICD installed).
RUNTIME_BUILDS: tuple[tuple[str, str, str, int], ...] = (
    (
        "vulkan",
        f"{_RELEASE_URL}/llama-{RUNTIME_RELEASE}-bin-win-vulkan-x64.zip",
        "7441fa34358d2501136e744fa7681b042591fa4e571bfe486f4cd8df58553797",
        33_560_206,
    ),
    (
        "cpu",
        f"{_RELEASE_URL}/llama-{RUNTIME_RELEASE}-bin-win-cpu-x64.zip",
        "2c23e78bafe6488dd28fad70af45f2770a998b062030c4af2212f436007810da",
        18_292_152,
    ),
)

_SERVER_BINARY = "llama-server.exe"


def supported_platform() -> bool:
    return platform.system() == "Windows" and platform.machine().lower() in (
        "amd64",
        "x86_64",
    )


def provisioning_root(home: str | Path | None = None) -> Path:
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / ".joiny-mnemonic"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(
    url: str,
    target: Path,
    sha256: str,
    *,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Fetch once, verify always; a mismatching file never lands on disk."""
    if target.exists():
        if _sha256(target) == sha256:
            return target
        raise ProvisioningError(
            "artifact_digest_mismatch",
            f"{target.name} exists but its sha256 does not match the pinned "
            "digest; delete it to re-download",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(url) as response, partial.open("wb") as handle:
            total = int(response.headers.get("content-length") or 0)
            step = max(total // 20, 1 << 24)
            marker = step
            for chunk in iter(lambda: response.read(1 << 20), b""):
                digest.update(chunk)
                handle.write(chunk)
                written += len(chunk)
                if progress and written >= marker:
                    marker += step
                    share = f"{100 * written // total}%" if total else f"{written}B"
                    progress(f"  {target.name}: {share}")
    except (urllib.error.URLError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise ProvisioningError(
            "artifact_unreachable", f"could not download {url}: {exc}"
        ) from exc
    if digest.hexdigest() != sha256:
        partial.unlink(missing_ok=True)
        raise ProvisioningError(
            "artifact_digest_mismatch",
            f"{target.name} downloaded with sha256 {digest.hexdigest()}, "
            f"expected {sha256}",
        )
    partial.replace(target)
    return target


def _runtime_usable(binary: Path) -> bool:
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def ensure_runtime(
    *,
    home: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Return a working llama-server binary, fetching it if necessary."""
    if not supported_platform():
        raise ProvisioningError(
            "platform_not_provisioned",
            f"automatic runtime provisioning is pinned for win-x64 only; on "
            f"{platform.system()}/{platform.machine()} install llama-server "
            "yourself and set the extractor backend block manually",
        )
    root = provisioning_root(home) / "runtime"
    failures: list[str] = []
    for name, url, sha256, _size in RUNTIME_BUILDS:
        directory = root / f"llama-{RUNTIME_RELEASE}-{name}"
        binary = directory / _SERVER_BINARY
        if not binary.exists():
            if progress:
                progress(f"fetching llama.cpp {RUNTIME_RELEASE} ({name})")
            archive = download(url, root / Path(url).name, sha256, progress=progress)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(directory)
            if not binary.exists():
                nested = next(directory.rglob(_SERVER_BINARY), None)
                if nested is not None:
                    binary = nested
        if binary.exists() and _runtime_usable(binary):
            return binary
        failures.append(name)
    raise ProvisioningError(
        "runtime_unusable",
        "no llama.cpp build could be started on this machine "
        f"(tried: {', '.join(failures)})",
    )


def ensure_model(
    key: str,
    *,
    home: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[ModelSpec, Path]:
    spec = MODEL_CATALOG.get(key)
    if spec is None:
        raise ProvisioningError(
            "unknown_model",
            f"unknown model {key!r}; available: {', '.join(sorted(MODEL_CATALOG))}",
        )
    target = provisioning_root(home) / "models" / spec.filename
    if not target.exists() and progress:
        progress(f"fetching {spec.title} ({spec.size_gb} GB)")
    return spec, download(spec.url, target, spec.sha256, progress=progress)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until_ready(endpoint: str, *, timeout: float = 180.0) -> None:
    """Block until the server answers, so callers never race a cold start."""
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{endpoint}/health", timeout=5) as response:
                if response.status == 200:
                    return
                last = f"status {response.status}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(1.0)
    raise ProvisioningError(
        "runtime_not_ready", f"llama-server did not become ready: {last}"
    )


def server_command(
    binary: Path, model_path: Path, spec: ModelSpec, port: int
) -> list[str]:
    return [
        str(binary),
        "--model",
        str(model_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--ctx-size",
        str(spec.context_tokens),
        "--n-gpu-layers",
        "999",
        "--jinja",
        "--log-disable",
    ]


def start_server(
    binary: Path,
    model_path: Path,
    spec: ModelSpec,
    *,
    port: int | None = None,
    log_path: Path | None = None,
) -> tuple[subprocess.Popen, str]:
    """Start the runtime and return it together with its loopback endpoint."""
    chosen = port or free_port()
    command = server_command(binary, model_path, spec, chosen)
    stream = open(log_path, "ab") if log_path else subprocess.DEVNULL
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdout=stream,
        stderr=subprocess.STDOUT if log_path else subprocess.DEVNULL,
        creationflags=creation,
        env={**os.environ, "LLAMA_LOG_COLORS": "0"},
    )
    endpoint = f"http://127.0.0.1:{chosen}"
    try:
        wait_until_ready(endpoint)
    except ProvisioningError:
        process.terminate()
        raise
    return process, endpoint


def backend_block(spec: ModelSpec, endpoint: str) -> dict:
    """The configuration block a provisioned model is served under.

    ``revision`` carries the full weight digest, so the evaluation identity
    moves whenever the actual bytes change — not merely when someone edits a
    label. It is not truncated: a report names the exact version it measured,
    and a shortened digest is a weaker statement than the one being claimed.
    """
    return BackendConfig(
        transport="openai_compatible",
        endpoint=f"{endpoint}/v1",
        model=spec.key,
        revision=f"sha256:{spec.sha256}",
        inference={"temperature": 0.0, "max_tokens": 768},
    ).descriptor()


def catalog_rows() -> Iterable[tuple[str, str, str]]:
    for key, spec in sorted(MODEL_CATALOG.items()):
        yield key, spec.title, f"{spec.size_gb} GB — {spec.notes}"


def provision(
    key: str = DEFAULT_MODEL,
    *,
    home: str | Path | None = None,
    port: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Fetch runtime + weights and record them in a ready-to-use state file.

    The port is fixed at provisioning time so the configured endpoint stays
    valid across sessions; the supervisor starts the server on that same port
    on demand.
    """
    binary = ensure_runtime(home=home, progress=progress)
    spec, model_path = ensure_model(key, home=home, progress=progress)
    chosen = port or free_port()
    endpoint = f"http://127.0.0.1:{chosen}"
    state = {
        "schema": "joiny-mnemonic-managed-runtime-v1",
        "model": spec.key,
        "title": spec.title,
        "model_path": str(model_path),
        "model_sha256": spec.sha256,
        "context_tokens": spec.context_tokens,
        "server_binary": str(binary),
        "runtime_release": RUNTIME_RELEASE,
        "port": chosen,
        "endpoint": endpoint,
        "backend": backend_block(spec, endpoint),
    }
    path = provisioning_root(home) / "managed-runtime.json"
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return state


def load_state(home: str | Path | None = None) -> dict | None:
    path = provisioning_root(home) / "managed-runtime.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
