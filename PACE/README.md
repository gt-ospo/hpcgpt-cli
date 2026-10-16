# PACE (Georgia Tech)

PACE deployment of hpcGPT for the Phoenix cluster.

## Setup

Run on the Phoenix login node. First time, get a compute-node allocation
first -- if `socat` isn't already available, `deploy.sh` builds it from
source, and that compiling shouldn't happen on the shared login node:

```bash
salloc --account=<slurm-account>
module load python # need recent Python
client-deployment/deploy.sh <slurm-account> # e.g., gts-gburdell3
```

socat only needs building once (cached under `.local/bin/`), so `salloc`
only matters for that first run -- later runs and opencode sessions don't
need it.

This installs opencode (if missing), sets up the Slurm MCP server's (in a venv),
writes an example `<slurm-account>.slurm_caps.json` file, and prints the env
vars to export before running `opencode`.

**NOTE:** The `.slurm_caps.json` file should be edited to place limits on usage! 

The MCP server provides sandboxed access to Slurm, with user-configurable caps
(limits) on the total amount of time jobs can use.

opencode itself is also sandboxed (see `opencode.jsonc`): its bash tool runs
through the `opencode-sandbox` plugin, and its edit tool is denied outright
on any `*.slurm_caps.json` file.

No LLM provider is configured, so this uses opencode's default free model
for now.
