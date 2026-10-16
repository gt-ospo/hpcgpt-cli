#!/usr/bin/env python3
# MCP server giving an LLM agent capped access to Slurm: sbatch/squeue/scancel
# only, routed through typed tool calls instead of a shell command line, so
# there's no CLI flag grammar to parse or get out of sync with.
#
# Caps come from a JSON config (see slurm_caps.example.json), one file per
# PACE project/account: a required top-level 'account' (fixed server-side,
# never agent-controlled), plus a 'qos' object mapping each QoS name (here,
# 'inferno' and 'embers') to a list of bands -- each band covers a set of
# partitions (or allow-all-partitions) and its own total walltime budget:
# before any sbatch call, the server sums the time limit of that band's
# currently pending+running jobs (via `squeue --json`) and rejects the
# submission if adding the requested time would exceed it (or no-limit to
# skip the check).
#
# Requires: Python 3.9+; pip install 'mcp<2' (uses the v1 FastMCP API --
# mcp 2.x renamed it to MCPServer with a different API).

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from mcp.server.fastmcp import FastMCP

ACTIVE_STATES = {"PENDING", "RUNNING", "CONFIGURING", "SUSPENDED"}
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
DURATION_RE = re.compile(
    r"^(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$"
)

USER = getpass.getuser()
mcp = FastMCP("slurm-capped")


class PartitionBand:
    def __init__(
        self,
        qos: str,
        partitions: Set[str],
        allow_all_partitions: bool,
        limit_seconds: Optional[int],
        no_limit: bool,
    ):
        self.qos = qos
        self.partitions = partitions
        self.allow_all_partitions = allow_all_partitions
        self.limit_seconds = limit_seconds
        self.no_limit = no_limit


CONFIG: Dict[str, List[PartitionBand]] = {}
# Path to this instance's state file, tracking job IDs it submitted itself --
# lets usage tracking/listing/cancellation ignore jobs submitted some other
# way (manually, or by a different slurm_mcp_server instance/config for a
# different project), without relying on a shared naming convention.
STATE_PATH: Path = None
# Charge account every job is submitted under -- fixed server-side via the
# config's top-level 'account', never a parameter the agent can set.
ACCOUNT: str = ""


def parse_duration(spec: str) -> int:
    match = DURATION_RE.match(spec.strip())
    if not match or not any(match.groups()):
        raise ValueError(f"invalid duration {spec!r}, expected e.g. '12h34m56s'")
    parts = match.groupdict(default="0")
    return (
        int(parts["days"]) * 86400
        + int(parts["hours"]) * 3600
        + int(parts["minutes"]) * 60
        + int(parts["seconds"])
    )


def load_config(path: str) -> Tuple[Dict[str, List[PartitionBand]], str]:
    # Enforced so opencode's permission.edit glob ("**/*.slurm_caps.json":
    # "deny") reliably blocks the agent's edit tool from touching this file
    # regardless of per-project prefix, without depending on which
    # directory it lives in.
    if not path.endswith(".slurm_caps.json"):
        raise ValueError(
            f"{path}: cap config filename must end with '.slurm_caps.json' "
            "(e.g. 'gts-myproject.slurm_caps.json')"
        )

    with open(path) as f:
        raw = json.load(f)

    account = raw.get("account")
    if not account:
        raise ValueError(f"{path}: top-level 'account' is required")

    configs: Dict[str, List[PartitionBand]] = {}
    for name, entries in raw.get("qos", {}).items():
        bands: List[PartitionBand] = []
        claimed: Set[str] = set()
        seen_allow_all = False

        for entry in entries:
            allow_all = bool(entry.get("allow-all-partitions", False))
            partitions = set(entry.get("partitions", []))
            if not allow_all and not partitions:
                raise ValueError(
                    f"qos.{name}: each band needs 'partitions' or 'allow-all-partitions'"
                )
            if allow_all:
                if seen_allow_all:
                    raise ValueError(f"qos.{name}: only one allow-all-partitions band allowed")
                if claimed:
                    raise ValueError(
                        f"qos.{name}: allow-all-partitions can't be combined with "
                        f"partition-specific bands in the same QoS (ambiguous which "
                        f"band governs {sorted(claimed)})"
                    )
                seen_allow_all = True
            else:
                if seen_allow_all:
                    raise ValueError(
                        f"qos.{name}: allow-all-partitions can't be combined with "
                        f"partition-specific bands in the same QoS (ambiguous which "
                        f"band governs {sorted(partitions)})"
                    )
                overlap = partitions & claimed
                if overlap:
                    raise ValueError(
                        f"qos.{name}: partitions {sorted(overlap)} claimed by multiple bands"
                    )
                claimed |= partitions

            no_limit = bool(entry.get("no-limit", False))
            limit_seconds = None
            if not no_limit:
                limit_raw = entry.get("limit")
                if limit_raw is None:
                    raise ValueError(f"qos.{name}: each band needs 'limit' or 'no-limit'")
                limit_seconds = parse_duration(limit_raw)

            bands.append(PartitionBand(name, partitions, allow_all, limit_seconds, no_limit))

        configs[name] = bands

    if not configs:
        raise ValueError(f"{path}: no [[qos.*]] bands defined")
    return configs, account


def find_band(qos: str, partition: str) -> PartitionBand:
    bands = CONFIG.get(qos)
    if bands is None:
        raise ValueError(f"unknown qos {qos!r}; configured: {sorted(CONFIG)}")

    catch_all = None
    for band in bands:
        if band.allow_all_partitions:
            catch_all = band
            continue
        if partition in band.partitions:
            return band
    if catch_all is not None:
        return catch_all

    allowed = sorted({p for b in bands for p in b.partitions})
    raise ValueError(f"partition {partition!r} not allowed under qos {qos!r}; allowed: {allowed}")


def sanitize_job_name(name: str) -> str:
    cleaned = SAFE_NAME_RE.sub("_", name).strip("_")
    return (cleaned or "agent-job")[:64]


def format_hms(total_seconds: int) -> str:
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def squeue_json(user: str) -> List[dict]:
    proc = subprocess.run(
        ["squeue", "--json", "-u", user],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout).get("jobs", [])


def job_state(job: dict) -> str:
    state = job.get("job_state")
    if isinstance(state, list):
        return state[0] if state else ""
    return state or ""


def load_state() -> Set[int]:
    try:
        with open(STATE_PATH) as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_state(job_ids: Set[int]) -> None:
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(sorted(job_ids), f)
    os.replace(tmp, STATE_PATH)


def record_submitted_job(job_id: int) -> None:
    state = load_state()
    state.add(job_id)
    save_state(state)


def managed_job_ids(all_jobs: List[dict]) -> Set[int]:
    """This instance's tracked job ids, pruned to ones still in squeue."""
    live_ids = {job.get("job_id") for job in all_jobs}
    state = load_state()
    pruned = state & live_ids
    if pruned != state:
        save_state(pruned)
    return pruned


def committed_seconds(user: str, qos: str, band: PartitionBand) -> int:
    all_jobs = squeue_json(user)
    managed = managed_job_ids(all_jobs)
    total = 0
    for job in all_jobs:
        if (
            job.get("job_id") not in managed
            or job.get("qos") != qos
            or job_state(job) not in ACTIVE_STATES
        ):
            continue
        try:
            job_band = find_band(qos, job.get("partition"))
        except ValueError:
            continue  # job's partition isn't covered by the current config; ignore
        if job_band is not band:
            continue
        time_limit = job.get("time_limit") or {}
        if time_limit.get("infinite"):
            continue
        total += int(time_limit.get("number", 0) or 0)
    return total


def build_script(
    job_name: str,
    account: str,
    workdir: str,
    partition: str,
    qos: str,
    time_seconds: int,
    nodes: int,
    ntasks: int,
    cpus_per_task: int,
    gpus: int,
    mem_mb: int,
    command: str,
) -> str:
    lines = [
        "#!/bin/sh",
        f"#SBATCH --job-name={sanitize_job_name(job_name)}",
        f"#SBATCH --account={account}",
        f"#SBATCH --chdir={workdir}",
        f"#SBATCH --partition={partition}",
        f"#SBATCH --qos={qos}",
        f"#SBATCH --time={format_hms(time_seconds)}",
        f"#SBATCH --nodes={nodes}",
        f"#SBATCH --ntasks={ntasks}",
        f"#SBATCH --cpus-per-task={cpus_per_task}",
    ]
    if gpus:
        lines.append(f"#SBATCH --gpus={gpus}")
    if mem_mb:
        lines.append(f"#SBATCH --mem={mem_mb}M")
    # This is the first non-#SBATCH, non-blank line, so sbatch's directive
    # scanner stops here -- anything inside the quoted command (even text
    # that looks like "#SBATCH ...") is just shell-quoted argument data to
    # `sh`, never examined as a script line by sbatch itself.
    #
    # TODO: `command` currently runs with the full filesystem access of the
    # submitting Unix user -- `--chdir` only sets the starting directory, it
    # doesn't confine anything. Restrict this to `workdir` only (e.g. wrap
    # in bwrap/apptainer bound to workdir, similar in spirit to
    # llm-sandbox.py's containment, but scoped per-job on the compute node).
    lines.append(f"sh -c {shlex.quote(command)}")
    return "\n".join(lines) + "\n"


@mcp.tool()
def sbatch_submit(
    command: str,
    qos: str,
    partition: str,
    time_seconds: int,
    nodes: int = 1,
    ntasks: int = 1,
    cpus_per_task: int = 1,
    gpus: int = 0,
    mem_mb: int = 0,
    job_name: str = "agent-job",
    workdir: Optional[str] = None,
) -> dict:
    """Submit a batch job. `command` runs via `sh -c` in a generated script;
    QoS/partition/walltime are validated against the server's cap config.
    mem_mb is memory per node in MB; 0 leaves it at the partition/QoS default."""
    band = find_band(qos, partition)

    if time_seconds <= 0:
        raise ValueError("time_seconds must be positive")
    if nodes <= 0 or ntasks <= 0 or cpus_per_task <= 0 or gpus < 0 or mem_mb < 0:
        raise ValueError(
            "nodes/ntasks/cpus_per_task must be positive, gpus/mem_mb must be >= 0"
        )
    if not command.strip():
        raise ValueError("command must be non-empty")

    if not band.no_limit:
        committed = committed_seconds(USER, qos, band)
        if committed + time_seconds > band.limit_seconds:
            raise ValueError(
                f"qos {qos!r} partition {partition!r} walltime budget exceeded: "
                f"{committed}s already committed + {time_seconds}s requested "
                f"> {band.limit_seconds}s limit"
            )

    workdir_path = Path(workdir).resolve() if workdir else Path.cwd()
    workdir_path.mkdir(parents=True, exist_ok=True)

    script = build_script(
        job_name, ACCOUNT, str(workdir_path), partition, qos, time_seconds,
        nodes, ntasks, cpus_per_task, gpus, mem_mb, command
    )
    fd, script_path = tempfile.mkstemp(
        prefix=".slurm_mcp_", suffix=".sh", dir=str(workdir_path)
    )
    with os.fdopen(fd, "w") as f:
        f.write(script)

    proc = subprocess.run(
        ["sbatch", "--parsable", script_path], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sbatch failed: {proc.stderr.strip()}")

    job_id = int(proc.stdout.strip().split(";")[0])
    record_submitted_job(job_id)

    return {
        "job_id": job_id,
        "script_path": script_path,
        "qos": qos,
        "partition": partition,
        "time_seconds": time_seconds,
    }


CLUSTER_DOCS = """\
# Phoenix Cluster Reference

## Detailed Node Specs

Most nodes share these common features:
- Dual Intel Xeon Gold 6226 CPUs @ 2.7 GHz (24 cores/node)
- DDR4-2933 MHz DRAM
- Infiniband 100HDR interconnect

Notable exceptions:

- **4 gpu-h100 nodes** (1 DGX added Nov 2023; 3 HGX added Jan 2024)
  - 1 DGX node: dual Intel Xeon Platinum 8480CL @ 2.0 GHz (112 cores/node), 8x Nvidia H100 80GB.
    Note: only available via the `embers` QoS for most Phoenix users.
  - 3 HGX nodes: dual Intel Xeon Platinum 8462Y+ @ 2.8 GHz (64 cores/node), 2TB DDR5 DRAM,
    4TB NVMe, 100 Gbps InfiniBand, 8x Nvidia H100 80GB.
- **12 gpu-h200 nodes**: dual Intel Xeon Platinum 8562Y @ 2.8 GHz (64 cores/node), 2TB DDR5-5600MHz
  DRAM, 4x200 Gbps InfiniBand, 28TB local NVMe, 8x Nvidia H200-HGX 142GB.
- **10 gpu-l40s nodes** (8 added Aug 30 2024): dual Intel Xeon 6426Y @ 2.5 GHz (32 cores/node),
  512GB DDR5 RAM, 61.44TB NVMe, 100 Gbps InfiniBand, 8x Nvidia L40S 48GB.
  Note: 8 of these are only available via `embers` for most Phoenix users; 2 are available via `inferno`.
- **54 cpu-gnr nodes** (added Oct 2025): dual Intel Xeon 6972P @ 3.8 GHz (192 cores/node),
  1.5 TiB DDR5 RAM, 4TB NVMe scratch, dual 400 Gbps InfiniBand, 8GB/CPU core.
- **3 gpu-rtxpro-blackwell nodes** (2 added Oct 2025, 1 added Feb 2026): dual 32-core Intel Xeon
  6737P @ 2.9 GHz (64 cores/node), 2TB DDR5 RAM, 7TB NVMe scratch, dual 400 Gbps InfiniBand,
  8x Nvidia RTX 6000 Pro Blackwell 96GB.
- **4 cpu-amd nodes** (added Nov 2023): dual AMD Epyc 9534 @ 2.45 GHz (128 cores/node),
  3TB DDR5 DRAM, 4TB NVMe, 100 Gbps InfiniBand.
- **40 cpu-large nodes** (added Feb 21 2023): dual Intel Xeon Gold 6226R @ 2.9 GHz (32 cores/node),
  768GB RAM.
- **12 gpu-a100 nodes** (5 added Nov 7 2022; 6 added Feb 23 2023; 1 added Feb 28 2023): dual AMD
  Epyc 7513 @ 2.6 GHz (64 cores/node), 512GB DDR4 DRAM, 2x Nvidia A100 40GB (6 nodes) or 80GB
  (6 nodes), 1.6TB NVMe.
- **1 additional gpu-a100 node** (added Apr 1 2024): dual AMD Epyc 7543 @ 2.8 GHz (64 cores/node),
  2TB DDR4 DRAM, 8x Nvidia A100 80GB, 180TB NVMe.
  Note: only available via `embers` for most Phoenix users.

### Node class table

| Node Class | Qty | RAM | Storage | CPU | GPU |
|---|---|---|---|---|---|
| CPU-192GB | 481 | 192 GB | 1.6 TB NVMe | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | — |
| CPU-384GB | 220 | 384 GB | 1.6 TB NVMe | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | — |
| CPU-768GB | 94 | 768 GB | 1.6 TB NVMe | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | — |
| CPU-384GB-SAS | 75 | 192 GB | 8.0 TB SAS | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | — |
| CPU-768GB-SAS | 4 | 384 GB | 8.0 TB SAS | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | — |
| CPU-GNR | 54 | 1.5 TB | 4 TB NVMe | Xeon 6972P (192 cores) | — |
| GPU-192GB-V100 | 9 | 192 GB | — | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | 2x Tesla V100 16/32GB |
| GPU-384GB-V100 | 33 | 384 GB | — | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | 2x Tesla V100 16/32GB |
| GPU-768GB-V100 | 5 | 768 GB | — | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | 2x Tesla V100 16GB |
| GPU-384GB-RTX6000 | 30 | 384 GB | — | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | 4x Quadro RTX6000 24GB |
| GPU-768GB-RTX6000 | 4 | 768 GB | — | 2x Xeon Gold 6226 @ 2.7GHz (24 cores) | 4x Quadro RTX6000 24GB |
| GPU-2TB-RTX-PRO-6000-BLACKWELL | 3 | 2 TB | 7 TB NVMe | Xeon 6737P (64 cores) | 8x RTX 6000 Pro Blackwell 96GB |
| CPU-3TB-AMD* | 4 | 3 TB | 4.0 TB NVMe | 2x AMD Epyc 9534 @ 2.45GHz (128 cores) | — |
| GPU-512GB-A100 | 12 | 512 GB | 1.6 TB NVMe | 2x AMD Epyc 7513 @ 2.6GHz (64 cores) | 2x A100 40/80GB |
| GPU-A100-HGX* | 1 | 2 TB | 180 TB NVMe | 2x AMD Epyc 7543 @ 2.8GHz (64 cores) | 8x A100 80GB |
| GPU-H100-HGX | 3 | 2 TB | 4.0 TB NVMe | 2x Xeon Platinum 8462Y+ @ 2.8GHz (64 cores) | 8x H100 80GB |
| GPU-H100-DGX* | 1 | 2 TB | — | 2x Xeon Platinum 8480CL @ 2.0GHz (112 cores) | 8x H100 80GB |
| GPU-H200-HGX | 12 | 2 TB | 28.0 TB NVMe | 2x Xeon Platinum 8562Y+ @ 2.8GHz (64 cores) | 8x H200 142GB |
| GPU-L40S | 10 | 512 GB | 61.44 TB NVMe | 2x Xeon 6426Y @ 2.5GHz (32 cores) | 8x L40S 48GB |

Total: ~1055 nodes** (varies with maintenance).

\\* Only available using the `embers` QoS for most Phoenix users.

## Partitions

- Jobs are assigned to Slurm partitions automatically based on charge account (internal/external)
  and the most significant resources requested (GPU, memory, etc).
- Jobs are only **charged** if the `inferno` QoS is selected.
- The assigned partition determines how much users are charged, based on current rates.
- The scheduler assigns a partition from the node classes below based on availability.

| Partition | Node Classes |
|---|---|
| cpu-small | CPU-192GB, CPU-384GB, CPU-384GB-SAS, CPU-768GB, CPU-768GB-SAS |
| cpu-medium | CPU-384GB, CPU-384GB-SAS, CPU-768GB, CPU-768GB-SAS |
| cpu-large | CPU-768GB, CPU-768GB-SAS |
| cpu-gnr | CPU-GNR |
| cpu-sas | CPU-384GB-SAS, CPU-768GB-SAS |
| cpu-pmem | CPU-PMEM |
| gpu-v100 | GPU-192GB-V100, GPU-384GB-V100, GPU-768GB-V100 |
| gpu-rtx6000 | GPU-384GB-RTX6000, GPU-768GB-RTX6000 |
| cpu-amd | CPU-512GB-AMD, CPU-3TB-AMD |
| gpu-a100 | GPU-512GB-A100 |
| gpu-h100 | GPU-H100-DGX, GPU-H100-HGX |
| gpu-l40s | GPU-L40S |
| gpu-rtxpro-blackwell | GPU-2TB-RTX-PRO-6000-BLACKWELL |
"""


@mcp.resource("slurm://cluster-docs")
def cluster_docs() -> str:
    """Cluster hardware and partition reference docs (node specs, which node
    classes back each partition, charging/QoS notes)."""
    return CLUSTER_DOCS


@mcp.resource("slurm://caps")
def caps_doc() -> str:
    """Configured QoS bands: which partitions each covers and their current
    walltime budgets, rendered from the live cap config."""
    lines = ["# Configured Slurm QoS caps", "", f"Charge account: `{ACCOUNT}`", ""]
    for qos, bands in sorted(CONFIG.items()):
        lines.append(f"## {qos}")
        for band in bands:
            scope = (
                "all partitions"
                if band.allow_all_partitions
                else ", ".join(sorted(band.partitions))
            )
            budget = (
                "no walltime limit"
                if band.no_limit
                else f"{format_hms(band.limit_seconds)} total walltime budget "
                f"across this band's in-flight (pending+running) jobs"
            )
            lines.append(f"- **{scope}**: {budget}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def squeue_list(qos: Optional[str] = None) -> List[dict]:
    """List pending/running jobs submitted via this server, optionally
    filtered by QoS."""
    all_jobs = squeue_json(USER)
    managed = managed_job_ids(all_jobs)
    out = []
    for job in all_jobs:
        if job.get("job_id") not in managed:
            continue
        if qos is not None and job.get("qos") != qos:
            continue
        time_limit = job.get("time_limit") or {}
        out.append(
            {
                "job_id": job.get("job_id"),
                "name": job.get("name"),
                "state": job_state(job),
                "partition": job.get("partition"),
                "qos": job.get("qos"),
                "time_limit_seconds": time_limit.get("number"),
            }
        )
    return out


@mcp.tool()
def scancel_job(job_id: int) -> dict:
    """Cancel a job. Refuses jobs not submitted via this server."""
    if job_id not in managed_job_ids(squeue_json(USER)):
        raise ValueError(f"job {job_id} was not submitted via this server")

    proc = subprocess.run(
        ["scancel", str(job_id)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"scancel failed: {proc.stderr.strip()}")
    return {"job_id": job_id, "cancelled": True}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=os.environ.get("SLURM_MCP_CONFIG", "default.slurm_caps.json"),
        help="path to JSON cap config, filename must end '.slurm_caps.json' "
        "(default: $SLURM_MCP_CONFIG or ./default.slurm_caps.json)",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("SLURM_MCP_STATE"),
        help="path to this instance's job-tracking state file "
        "(default: $SLURM_MCP_STATE, or --config with a .state.json suffix)",
    )
    args = parser.parse_args()

    global CONFIG, ACCOUNT, STATE_PATH
    CONFIG, ACCOUNT = load_config(args.config)
    STATE_PATH = Path(args.state_file) if args.state_file else Path(args.config).with_suffix(".state.json")
    mcp.run()


if __name__ == "__main__":
    main()
