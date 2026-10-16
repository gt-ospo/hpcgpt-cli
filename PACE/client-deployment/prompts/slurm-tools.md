## Slurm access

Use the `slurm-mcp-server` MCP tools for all Slurm job management:
`sbatch_submit`, `squeue_list`, `scancel_job`. Don't run `sbatch`, `salloc`,
`srun`, `squeue`, `sinfo`, `scancel`, `scontrol`, or `sacct` directly via the
shell -- those are denied and will fail. Use the `slurm://cluster-docs` and
`slurm://caps` resources for cluster/partition reference and current QoS
budgets instead of `sinfo`.
