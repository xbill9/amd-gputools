---
name: remote-sync
description: Copy this working tree to the AMD GPU droplet and run a command there. Use when a change has to be exercised on real MI300 hardware, which is the only place it can run.
disable-model-invocation: true
---

# remote-sync

Push the current working tree to the project droplet and run something on it.

`$ARGUMENTS` is the command to run on the droplet. With no arguments, sync only.

There is no AMD GPU on this machine, so this is how local code is exercised at all.
`rsync` is **not installed here** — do not reach for it. Sync with `tar` over `ssh`,
which needs nothing on either end beyond `tar` and the SSH access the MCP server
already uses.

## Steps

1. **Find the droplet.** Call `mcp__amd-gputools__list_droplets`. If more than one is
   tagged, ask which. If none is, stop and say so — the tag is `DROPLET_TAG` in `amd.env`.

2. **Make sure it is up.** If the droplet is not `active`, call
   `mcp__amd-gputools__start_droplet` and poll `mcp__amd-gputools__droplet_status` until
   it is. Boot takes a minute or two; the public IP appears as it comes up.

3. **Get the address** from `mcp__amd-gputools__ssh_command`. Never reuse an IP from an
   earlier session — a rebuilt droplet has a different one.

4. **Sync.** From the project root, excluding everything that must not travel:

   ```
   tar czf - --exclude=.git --exclude=.env --exclude=__pycache__ \
       --exclude=.ruff_cache --exclude=run . \
     | ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new root@<ip> \
         'mkdir -p /opt/amd-gputools && tar xzf - -C /opt/amd-gputools'
   ```

   `.env` is excluded deliberately: it holds the live DigitalOcean token and has no
   business on a machine that is billed by the hour and may be rebuilt. If the droplet
   needs its own credentials, put them there directly.

   This overlays the tree — it does not delete files the droplet has and the local tree
   does not. When stale remote files matter, remove the remote directory first and say
   that you are doing it.

5. **Run it.** Pass `$ARGUMENTS` to `mcp__amd-gputools__run_on_droplet` with
   `cd /opt/amd-gputools && <command>`. Raise `timeout` above the 300s default for
   anything that compiles or benchmarks.

6. **Report** the exit code and the real output. A failing command on the droplet is a
   result, not something to retry locally — it cannot run locally.

## Do not

- Do not fall back to running the command locally when the droplet is unreachable.
  Say it is unreachable.
- Do not sync `.git`. Once the GitHub remote exists, `git pull` on the droplet is the
  better path for anything long-lived, and this skill is for uncommitted work.
