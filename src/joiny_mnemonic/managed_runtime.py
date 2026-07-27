"""Keep the provisioned extractor runtime available without the user running it.

The installer provisions weights and a server binary and pins an endpoint. From
then on the first extraction that needs the model starts it; a runtime that is
already answering is reused. Nothing here starts a server the user did not
provision, and nothing starts one at an endpoint the configuration does not
already name.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from .model_provisioning import (
    MODEL_CATALOG,
    ProvisioningError,
    provisioning_root,
    server_command,
    wait_until_ready,
)


def is_healthy(endpoint: str, *, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _matches(backend: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    """The state may only supervise the exact endpoint the config points at."""
    return str(backend.get("endpoint", "")).rstrip("/") == (
        f"{state.get('endpoint', '')}/v1"
    )


def ensure_running(
    backend: Mapping[str, Any] | None,
    *,
    home: str | Path | None = None,
    state: Mapping[str, Any] | None = None,
    startup_timeout: float = 300.0,
) -> str | None:
    """Return the live endpoint, starting the provisioned runtime if needed.

    Returns ``None`` when the configured backend is not the provisioned one —
    a hand-managed server stays entirely the user's business.
    """
    if not backend:
        return None
    if state is None:
        from .model_provisioning import load_state

        state = load_state(home)
    if not state or not _matches(backend, state):
        return None
    endpoint = str(state["endpoint"])
    if is_healthy(endpoint):
        return endpoint
    spec = MODEL_CATALOG.get(str(state.get("model")))
    if spec is None:
        raise ProvisioningError(
            "unknown_model",
            f"provisioned model {state.get('model')!r} is not in the catalog",
        )
    model_path = Path(state["model_path"])
    if not model_path.exists():
        raise ProvisioningError(
            "weights_missing",
            f"provisioned weights are gone: {model_path}; re-run the installer",
        )
    logs = provisioning_root(home) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    command = server_command(
        Path(state["server_binary"]), model_path, spec, int(state["port"])
    )
    detached = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    with (logs / "llama-server.log").open("ab") as log:
        subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=detached,
            close_fds=True,
        )
    # If a concurrent start won the port, this simply waits for that instance.
    wait_until_ready(endpoint, timeout=startup_timeout)
    return endpoint
