"""CityGML importer — triggers citydb-tool via Python Docker SDK."""

import os
import re
import socket
from pathlib import Path
from typing import Generator

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


DATA_DIR = Path(__file__).parent.parent / "data"
CITYDB_TOOL_IMAGE = "ghcr.io/3dcitydb/citydb-tool"
TILER_IMAGE = "ghcr.io/tum-gis/citydb-3dtiler:latest"
TILER_OUTPUT_PATH = "/home/tester/citydb-3dtiler/shared"  # inside the tiler container


def _network_name(client=None) -> str:
    """Resolve the Docker network the citydb-tool container should join.

    Falls back to env var or auto-detects from this container's own networks.
    """
    override = os.environ.get("CITYDB_DOCKER_NETWORK")
    if override:
        return override

    if client is not None:
        try:
            container = client.containers.get(socket.gethostname())
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            # Pick the first non-default network (the user-defined citydb-net).
            for name in networks:
                if name not in ("bridge", "host", "none"):
                    return name
        except Exception:
            pass

    return "production_citydb-net"


def _host_data_dir(client) -> str:
    """Resolve the HOST path for /app/data so it can be passed to the Docker daemon.

    Inside the agent container, /app/data is a bind mount from a host directory
    (e.g. ./production/data/). Sibling containers launched via the host's Docker
    daemon must mount the host path, not /app/data.

    Order of preference:
      1. CITYDB_HOST_DATA_DIR env var (explicit override).
      2. Inspect our own container via the Docker socket.
      3. Fall back to DATA_DIR (works when running outside Docker).
    """
    override = os.environ.get("CITYDB_HOST_DATA_DIR")
    if override:
        return override

    try:
        container_id = socket.gethostname()
        container = client.containers.get(container_id)
        for mount in container.attrs.get("Mounts", []):
            if mount.get("Destination") == "/app/data":
                source = mount.get("Source")
                if source:
                    return source
    except Exception:
        pass

    return str(DATA_DIR.resolve())


def _db_env() -> dict:
    """Environment variables for the citydb-tool container (CITYDB_* naming convention)."""
    return {
        "CITYDB_HOST": os.environ.get("CITYDB_HOST", "postgres"),
        "CITYDB_PORT": os.environ.get("CITYDB_PORT", "5432"),
        "CITYDB_NAME": os.environ.get("CITYDB_NAME", os.environ.get("POSTGRES_DB", "citydb")),
        "CITYDB_SCHEMA": os.environ.get("CITYDB_SCHEMA", "citydb"),
        "CITYDB_USERNAME": os.environ.get("CITYDB_USER", os.environ.get("POSTGRES_USER", "citydb")),
        "CITYDB_PASSWORD": os.environ.get("CITYDB_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "citydb")),
    }


def _tiles_volume_name(client) -> str:
    """Find the Docker volume name backing /tiles in this container.

    Inspects own mounts via the Docker socket; falls back to the default
    Compose-generated name so it works even outside Docker.
    """
    try:
        container = client.containers.get(socket.gethostname())
        for mount in container.attrs.get("Mounts", []):
            if mount.get("Destination") == "/tiles" and mount.get("Type") == "volume":
                return mount["Name"]
    except Exception:
        pass
    return "production_tiles_data"


_SUPPORTED_EXTENSIONS = (".gml", ".xml", ".citygml", ".json", ".jsonl", ".gz", ".gzip", ".zip")


def _detect_format(filename: str) -> str:
    """Return 'cityjson' or 'citygml' based on file extension.

    For compressed files (.gz, .gzip, .zip), the inner extension is checked
    (e.g. 'model.json.gz' → cityjson). Plain archives with no recognisable
    inner extension default to 'citygml'.
    """
    name = filename.lower()
    for ext in (".gz", ".gzip", ".zip"):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    if name.endswith((".json", ".jsonl")):
        return "cityjson"
    return "citygml"


def list_gml_files() -> list[str]:
    if not DATA_DIR.exists():
        return []
    return sorted(
        f.name for f in DATA_DIR.iterdir()
        if f.suffix.lower() in _SUPPORTED_EXTENSIONS
    )


def run_tiler() -> Generator[str, None, None]:
    """Run the citydb-3dtiler Docker container and stream its output.

    Pulls the image if not present, then runs the tiler with tty=True so
    progress output is flushed line-by-line. Writes tiles into the same
    'tiles_data' Docker volume that the agent serves at /tiles.
    """
    try:
        import docker
    except ImportError:
        yield "ERROR: 'docker' Python package not installed.\n"
        return

    try:
        client = docker.from_env()
    except Exception as exc:
        yield f"ERROR: Cannot connect to Docker socket: {exc}\n"
        return

    # ── Pull image if not present locally ────────────────────────────────────
    yield f"Checking image {TILER_IMAGE}...\n"
    try:
        client.images.get(TILER_IMAGE)
        yield "  Image already present locally.\n"
    except docker.errors.ImageNotFound:
        yield "  Image not found locally — pulling from registry (this may take a few minutes)...\n"
        try:
            seen_layers: set = set()
            for event in client.api.pull(TILER_IMAGE, stream=True, decode=True):
                status = event.get("status", "")
                layer = event.get("id", "")
                progress = event.get("progressDetail", {})
                if status == "Pull complete" and layer not in seen_layers:
                    seen_layers.add(layer)
                    yield f"  ✓ Layer {layer}\n"
                elif status == "Downloading" and progress.get("total"):
                    done = progress.get("current", 0)
                    total = progress["total"]
                    pct = int(done * 100 / total)
                    yield f"\r  Downloading {layer}: {pct}%"
                elif status not in ("Waiting", "Pulling fs layer", "Downloading", ""):
                    yield f"  {layer} {status}\n".strip() + "\n"
            yield "  Pull complete.\n"
        except Exception as exc:
            yield f"  WARNING: Pull failed ({exc}) — trying with local image if available.\n"

    # ── Resolve runtime params ────────────────────────────────────────────────
    volume_name = _tiles_volume_name(client)
    network = _network_name(client)
    db = _db_env()

    yield (
        f"\nConfiguration:\n"
        f"  DB       : {db['CITYDB_HOST']}:{db['CITYDB_PORT']}"
        f"/{db['CITYDB_NAME']} (schema: {db.get('CITYDB_SCHEMA', 'citydb')})\n"
        f"  Volume   : {volume_name} → {TILER_OUTPUT_PATH}\n"
        f"  Network  : {network}\n"
        f"\nStarting tiler — output below:\n"
        f"{'─' * 60}\n"
    )

    cmd = (
        f"--db-host {db['CITYDB_HOST']} "
        f"--db-port {db['CITYDB_PORT']} "
        f"--db-name {db['CITYDB_NAME']} "
        f"--db-schema {db.get('CITYDB_SCHEMA', 'citydb')} "
        f"--db-username {db['CITYDB_USERNAME']} "
        f"--db-password {db['CITYDB_PASSWORD']} "
        f"tile"
    )

    try:
        container = client.containers.run(
            image=TILER_IMAGE,
            command=cmd,
            volumes={volume_name: {"bind": TILER_OUTPUT_PATH, "mode": "rw"}},
            network=network,
            tty=True,        # matches `docker run -t`; flushes progress output line-by-line
            stdin_open=True,  # matches `docker run -i`
            detach=True,
            remove=False,
        )
    except Exception as exc:
        yield f"ERROR: Failed to start tiler container: {exc}\n"
        return

    yield f"Container ID: {container.short_id}\n"

    try:
        # With tty=True logs() returns a raw byte stream (no demux headers needed)
        for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
            text = _ANSI_RE.sub("", chunk.decode("utf-8", errors="replace"))
            if text:
                yield text

        result = container.wait()
        exit_code = result.get("StatusCode", -1)
        yield f"\n{'─' * 60}\n"
        if exit_code == 0:
            yield "3D tile generation finished successfully.\n"
        else:
            yield f"Tiler exited with code {exit_code}.\n"
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


def import_city_file(filename: str, fmt_override: str = "auto") -> Generator[str, None, None]:
    try:
        import docker
    except ImportError:
        yield "ERROR: 'docker' Python package not installed. Run: pip install docker\n"
        return

    if not filename:
        yield "ERROR: No file selected.\n"
        return

    # Strip any directory components from the supplied name. Without this, a
    # name like "../../etc/passwd" would resolve outside DATA_DIR.
    from pathlib import Path as _Path
    safe_name = _Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        yield f"ERROR: Invalid filename: {filename!r}\n"
        return
    if safe_name != filename:
        yield f"ERROR: Filename must not contain path components: {filename!r}\n"
        return
    filename = safe_name

    gml_path = (DATA_DIR / filename).resolve()
    # Defence-in-depth: ensure resolved path stays under DATA_DIR.
    try:
        gml_path.relative_to(DATA_DIR.resolve())
    except ValueError:
        yield f"ERROR: Filename escapes data directory: {filename!r}\n"
        return
    if not gml_path.exists():
        yield f"ERROR: File not found: {gml_path}\n"
        return

    if fmt_override in ("citygml", "cityjson"):
        fmt = fmt_override
        yield f"Starting import of {filename} (format: {fmt}, manually selected)...\n"
    else:
        fmt = _detect_format(filename)
        _lower = filename.lower()
        _ambiguous = _lower.endswith(".zip") and not (
            _lower.endswith(".json.zip") or _lower.endswith(".jsonl.zip")
        )
        if _ambiguous:
            yield (
                f"Starting import of {filename} (format: {fmt}, auto-detected)...\n"
                f"  ⚠ ZIP archive detected — defaulted to {fmt}. "
                f"Use the Format selector if the archive contains "
                f"{'CityGML' if fmt == 'cityjson' else 'CityJSON'} files instead.\n"
            )
        else:
            yield f"Starting import of {filename} (format: {fmt}, auto-detected)...\n"

    try:
        client = docker.from_env()
    except Exception as exc:
        yield f"ERROR: Cannot connect to Docker socket: {exc}\n"
        return

    host_data_dir = _host_data_dir(client)
    network = _network_name(client)
    yield f"Mounting host directory {host_data_dir} -> /data, network: {network}\n"

    try:
        container = client.containers.run(
            image=CITYDB_TOOL_IMAGE,
            command=f"import {fmt} /data/{filename}",
            environment=_db_env(),
            volumes={host_data_dir: {"bind": "/data", "mode": "rw"}},
            network=network,
            detach=True,
            remove=False,
        )
    except Exception as exc:
        yield f"ERROR: Failed to start container: {exc}\n"
        return

    try:
        for log_bytes in container.logs(stream=True, follow=True):
            line = log_bytes.decode("utf-8", errors="replace")
            yield line

        result = container.wait()
        exit_code = result.get("StatusCode", -1)
        if exit_code == 0:
            yield "\nImport finished successfully.\n"
        else:
            yield f"\nImport exited with code {exit_code}.\n"
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass
