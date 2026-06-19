# Mount the Ferma S3 (TWC) bucket as a Windows drive — task spec for an agent

> **Read this caveat first.** Mounting does **not** speed up downloads. The
> bottleneck is the network route from this host (Vietnam / VNPT) to the TWC
> storage endpoint in Russia: ~267 ms RTT, ~40 KiB/s single-stream — confirmed
> independently with AWS CLI (`aws s3 cp` → ~40 KiB/s) and plain `urllib`.
> Multipart/parallel made it **worse** in benchmarks (congestion collapse on the
> high-latency international leg). A mount just changes the *interface*, not the
> throughput.
>
> The mount is worth doing **only** combined with **pre-fetch / caching**:
> download videos to a local cache **ahead of time / in the background** (during
> good windows), so the poster reads already-local files and never blocks on the
> slow link. See "Recommended use" at the bottom.

## Connection parameters (TWC Storage, S3-compatible)

| | |
|---|---|
| type | `s3` (provider: Other / S3-compatible) |
| endpoint | `https://s3.twcstorage.ru` |
| region | `ru-1` |
| bucket | `neiroslop` |
| video prefix | `ferma/` (one folder per video_id, `VID_*.mp4` + `meta.json`) |
| access key id | `JPVJBENRRPBNTYBVY56O` |
| secret access key | in the repo `.env` → `FERMA_S3_SECRET_KEY` (do not hard-code in a committed file) |
| addressing | **path-style** (bucket in the path, not a subdomain) |

## Step 1 — install WinFsp + rclone

```powershell
winget install -e --id WinFsp.WinFsp
winget install -e --id Rclone.Rclone
# verify
rclone version
```
(Or download: WinFsp https://winfsp.dev/rel/ , rclone https://rclone.org/downloads/ )

## Step 2 — configure the rclone remote

Create/append to `%USERPROFILE%\AppData\Roaming\rclone\rclone.conf`
(or run `rclone config` interactively). Put the **secret** here from `.env`,
not in any committed file:

```ini
[twc]
type = s3
provider = Other
access_key_id = JPVJBENRRPBNTYBVY56O
secret_access_key = <value of FERMA_S3_SECRET_KEY from .env>
endpoint = https://s3.twcstorage.ru
region = ru-1
force_path_style = true
```

Smoke-test (no mount yet):
```powershell
rclone lsd twc:neiroslop/ferma           # list folders
rclone ls  twc:neiroslop/ferma/21_kor    # list one folder's files
```

## Step 3 — mount as a drive

```powershell
rclone mount twc:neiroslop X: ^
  --vfs-cache-mode full ^
  --vfs-cache-max-age 168h ^
  --vfs-cache-max-size 50G ^
  --dir-cache-time 24h ^
  --vfs-read-chunk-size 16M ^
  --vfs-read-chunk-size-limit off ^
  --network-mode
```
Now `X:\ferma\21_kor\VID_*.mp4` is browsable. First read of any file still
downloads at link speed (slow); subsequent reads come from the local VFS cache
at `%LOCALAPPDATA%\rclone\vfs`.

Run it as a background/at-boot service (NSSM or a scheduled task) so the mount
survives logoff. Keep `--vfs-cache-mode full` so writes/reads are cached on disk.

## Recommended use — pre-fetch, don't read live

Because the link is slow but videos aren't needed instantly, **warm the cache in
advance** instead of reading on the critical path:

- Periodically pre-copy upcoming videos to local disk in the background, one at a
  time (serial — concurrent downloads collapse this route):
  ```powershell
  rclone copy twc:neiroslop/ferma/21_kor C:\fffbt_cache\21_kor ^
    --transfers 1 --multi-thread-streams 1 --retries 10 --low-level-retries 20
  ```
  `--transfers 1` is deliberate: parallel transfers make this route slower.
- Then point the poster at the local copies (e.g. via `local_video_path` instead
  of an S3 URL), so `VideoPreparationStep` skips the download entirely.

This decouples the slow VN→RU transfer from the posting pipeline: downloads run
ahead of time / overnight; posting always uses local files.

## The real fix (separate from this)

The mount/cache is a workaround. The durable fix is to remove the VN→RU leg:
a storage endpoint/CDN closer to Vietnam, mirroring videos to an Asian region,
or a relay host with good RU peering. That restores real throughput; the mount
only hides the latency behind a cache.
