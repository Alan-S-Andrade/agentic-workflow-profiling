# Autonomous SWE-bench microVM replay

Each session is a Cloud Hypervisor VM with two disks:

1. The read-only offline base disk contains the guest init process and
   `local-replay.py`.
2. The private writable disk is an exported official SWE-bench instance image
   with `/replay/manifest.json` installed.

At boot, guest init runs `local-replay.py`. It sleeps for every saved model
inference span from the assigned `/yig` trajectory and executes that turn's
saved Bash commands inside `chroot /rw`. It writes tool records and a summary
to the private disk, synchronizes, and powers off.

The host creates the disk, starts the VM, samples the VMM cgroup, and reads the
completed summary after shutdown. It has no vsock device, guest listener, or
post-boot command path.

Each VM sees every CPU in the configured Node-0 CPU set and has no `cpu.max`
quota. The host scheduler allocates CPU time to runnable guest threads, so a
session can use more CPUs as its tools create parallel work and gives those
cycles back when idle. This is shared, overcommitted CPU capacity rather than
a fixed per-VM vCPU entitlement.

Memory is also demand-backed. By default a VM sees all Node-0 RAM as its
guest-memory ceiling and has no `memory.max` cgroup limit. This does not
reserve all of that RAM: its actual resident memory grows as its tools, test
processes, caches, and page tables touch memory. Admission stops globally at
the Node-0 memory or CPU threshold, which lets the experiment measure each
session's natural resource demand rather than a fixed VM quota.

Rebuild the base disk after changing guest files:

```bash
sudo install -m 0755 microvm_assets/local_replay.py microvm-assets/rootfs-tree/usr/local/bin/local-replay.py
sudo install -m 0755 microvm_assets/init microvm-assets/rootfs-tree/sbin/init
sudo mkfs.ext4 -F -q -d microvm-assets/rootfs-tree microvm-assets/replay-rootfs.img
```

Create a plan for all available distinct trajectories:

```bash
python3 run_yig_microvm_replay.py --output traces/yig-swebench-verified-microvm
```

Run a small staging batch:

```bash
python3 run_yig_microvm_replay.py \
  --output traces/yig-swebench-stage \
  --sessions 10 --run
```

## Remote-inference scale variant

`swebench_microvm_scale_experiment.py` uses the same official SWE-bench OCI
images but does not package `/yig` trajectories or replay manifests into the
VMs. The guest gets a private NATed network interface and requests turns from
the remote trace-backed inference service. The default endpoint is
`http://130.127.133.251:8080/v1/inference`; override it with
`--inference-endpoint` or `MICROVM_INFERENCE_ENDPOINT`.

The service exposes `GET /v1/instances`, returning either a JSON list of
instance IDs or `{ "instances": [...] }`, and accepts turn requests such as:

```json
{"instance_id":"...","session_id":"...","turn":1}
```

It returns the exact turn's Bash commands and a completion flag:

```json
{"turn":1,"commands":["..."],"done":false,"inference_seconds":0.0}
```

The guest records actual request latency; it does not sleep on
`inference_seconds`. The host therefore measures remote inference as a real
networked dependency rather than simulating a saved model delay.
