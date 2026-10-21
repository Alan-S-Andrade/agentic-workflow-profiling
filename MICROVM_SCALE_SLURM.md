# GH200 Slurm client sweep

This is the rootless GH200/Vista counterpart to `setup_microvm_scale_client.sh`. It provisions Cloud Hypervisor and a small guest filesystem in the Slurm job, then runs the full concurrency sweep against the remote trace server. The guest reaches the server through Cloud Hypervisor vsock; it does not need a TAP device, bridge, NAT rules, or sudo.

Submit from a TACC login node, with the checkout on a filesystem visible to compute nodes:

```bash
cd /path/to/agentic-workflow-profiling
sbatch slurm/run_microvm_scale_client.slurm
```

The job defaults to `http://128.110.220.114:8080/v1/inference` and requests ARM64 images with `?arch=arm64`. Before submitting, update the running trace server to the version of `swebench_microvm_scale_experiment.py` in this checkout, then restart that server. The updated handler must be running and the server must be able to pull `swebench/sweb.eval.arm64.<instance>:latest` Docker images. The old handler ignores `arch` and serves x86_64 images, which cannot run in GH200 guests. Do this server deployment on the server machine; the Slurm setup does not change or restart it.

To override the server address:

```bash
sbatch --export=ALL,SERVER_IP=128.110.220.115 slurm/run_microvm_scale_client.slurm
```

The batch file uses the Vista GH200 settings recorded in `ncar_run/sdebench-mt/ghconfig.md`: partition `gh`, account `ASC26114`, one node, 72 CPUs, and a 24 hour limit. It loads `tacc-apptainer`, downloads the pinned Cloud Hypervisor and guest kernel, builds the guest sandbox with Apptainer fakeroot, and checks `/dev/kvm` plus the server health endpoint before running stages 1 through 128.

Monitor and inspect the run:

```bash
squeue -u "$USER"
tail -f microvm-scale-JOBID.out
ls traces/microvm-scale-JOBID
```

The job fails early if it lands on a node without readable and writable `/dev/kvm`, if the Apptainer build fails, or if the server health check fails. The default 24 hour job limit can be changed in the `#SBATCH -t` line. The run allows the node's configured swap and records per-process Cloud Hypervisor PSS because unprivileged jobs cannot disable swap or create private cgroups. The complete sweep launches the 128-VM stage; its runtime and memory use depend on the server's catalog and node load.
