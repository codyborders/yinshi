# Runner storage options

Yinshi can run with no runner at all, with an AWS-native bring-your-own-cloud runner, or with experimental Archil-backed POSIX storage. The choice controls where live SQLite, Git worktrees, Pi configuration, session files, artifacts, and backups live.

## Quick choice guide

| Option | Status | Runner required | Storage ownership | Choose this when |
|---|---|---:|---|---|
| Hosted Yinshi | Supported | No | Existing Yinshi hosted deployment storage | You want the current product with no cloud setup. |
| AWS BYOC: EBS plus S3 Files | Supported | Yes | Active compute and storage stay in your AWS account | You want the strongest BYOC story and AWS-native operations. |
| Archil shared-files mode | Experimental | Yes | SQLite stays on AWS EBS; shared files use Archil-managed POSIX storage | You already trust Archil or want elastic POSIX shared files. |
| Archil all-POSIX mode | Experimental | Yes | SQLite and shared files use Archil-managed POSIX storage | You accept experimental storage while Yinshi stress-tests SQLite and Git workloads. |

## Profile values and default paths

| Profile value | SQLite storage | Shared files storage | Default SQLite path | Default shared path | Live SQLite under shared path |
|---|---|---|---|---|---:|
| `aws_ebs_s3_files` | `runner_ebs` | `s3_files_or_local_posix` | `/var/lib/yinshi/sqlite` | `/mnt/yinshi-s3-files` | No |
| `archil_shared_files` | `runner_ebs` | `archil` | `/var/lib/yinshi/sqlite` | `/mnt/archil/yinshi` | No |
| `archil_all_posix` | `archil` | `archil` | `/mnt/archil/yinshi/sqlite` | `/mnt/archil/yinshi` | Yes |

The runner token file defaults to `/var/lib/yinshi/runner-token` for every profile. Bearer material does not need to live on shared storage.

## Hosted Yinshi

Hosted Yinshi is the current product path. Do not create a runner registration token for this option. Yinshi uses its existing hosted runtime and storage.

Use this option when you want the least setup and do not need user-owned AWS compute.

## AWS BYOC: EBS plus S3 Files

This is the supported runner-backed profile. The EC2 runner, encrypted root EBS volume, optional S3 Files mount, and active data paths are all in the user's AWS account.

Default behavior:

- `YINSHI_RUNNER_STORAGE_PROFILE=aws_ebs_s3_files`
- `YINSHI_RUNNER_SQLITE_STORAGE=runner_ebs`
- `YINSHI_RUNNER_SHARED_FILES_STORAGE=s3_files_or_local_posix`
- `YINSHI_RUNNER_SQLITE_DIR=/var/lib/yinshi/sqlite`
- `YINSHI_RUNNER_SHARED_FILES_DIR=/mnt/yinshi-s3-files`

Live SQLite must not be placed under the shared files root for this profile. The control plane rejects layouts such as `/mnt/yinshi-s3-files/sqlite` because S3-style or NFS-style shared mounts are not the safe default for SQLite WAL behavior.

`SharedFilesMountCommand` in `aws-runner-cloudformation.yaml` can mount S3 Files, NFS, or another POSIX-compatible filesystem at `/mnt/yinshi-s3-files`. If it is empty, the template leaves a local EBS-backed directory at that path.

## Archil shared-files mode

This experimental profile keeps live SQLite on runner EBS and places shared project files on Archil POSIX storage.

Default behavior:

- `YINSHI_RUNNER_STORAGE_PROFILE=archil_shared_files`
- `YINSHI_RUNNER_SQLITE_STORAGE=runner_ebs`
- `YINSHI_RUNNER_SHARED_FILES_STORAGE=archil`
- `YINSHI_RUNNER_SQLITE_DIR=/var/lib/yinshi/sqlite`
- `YINSHI_RUNNER_SHARED_FILES_DIR=/mnt/archil/yinshi`

The runner must explicitly advertise `YINSHI_RUNNER_SHARED_FILES_STORAGE=archil`. A mounted directory alone is not enough evidence for the control plane to distinguish Archil from S3 Files, NFS, or local POSIX storage.

Live SQLite must still stay outside the shared root. The control plane rejects `/mnt/archil/yinshi/sqlite` for this profile.

## Archil all-POSIX mode

This experimental profile places both live SQLite and shared project files on Archil.

Default behavior:

- `YINSHI_RUNNER_STORAGE_PROFILE=archil_all_posix`
- `YINSHI_RUNNER_SQLITE_STORAGE=archil`
- `YINSHI_RUNNER_SHARED_FILES_STORAGE=archil`
- `YINSHI_RUNNER_SQLITE_DIR=/mnt/archil/yinshi/sqlite`
- `YINSHI_RUNNER_SHARED_FILES_DIR=/mnt/archil/yinshi`

Use this only when you accept the current experimental risk. Yinshi still needs workload-specific stress testing for SQLite WAL behavior, crash recovery, concurrent readers, `PRAGMA integrity_check`, Git checkout workloads, and Pi session append patterns before this profile should be considered production-supported.

## Archil ownership and account requirements

Standard Archil is not pure BYOC in the same sense as an EBS volume and S3 bucket that live entirely in the user's AWS account. A user typically needs:

- An Archil account.
- An Archil disk.
- An Archil API token, disk token, or cloud authorization path managed outside Yinshi.
- A mount command or provisioning script that mounts the disk on the runner host.
- Optional user-owned S3, R2, or GCS backing storage connected as an Archil data source.

The important ownership distinction is that standard Archil uses an Archil-managed active storage/cache layer. Durable backing data can be connected to a user-owned bucket, but active POSIX storage is still operated by Archil unless the user has a separate enterprise BYOC arrangement with Archil.

Do not paste long-lived Archil secrets into this repository or into a shared CloudFormation template. Store secrets in AWS Secrets Manager, SSM Parameter Store, your own bootstrap scripts, or another secret store you control.

## Multi-runner Archil notes

If multiple runners use one Archil disk, keep each runner in a disjoint subtree unless you have reviewed Archil's shared-disk ownership rules. Shared write ownership matters for Git worktrees, SQLite databases, Pi session files, and backup paths.

For clean shutdown customization, unmount Archil with `archil unmount` so pending writes can flush before the instance stops. Add that shutdown behavior to your own systemd unit or lifecycle hook if you run long-lived Archil-backed runners.

## CloudFormation usage

Use `docs/deployment/aws-runner-cloudformation.yaml` for runner launch. Set `RunnerStorageProfile` to one of the profile values above.

For AWS BYOC, `SharedFilesMountCommand` can be empty during early testing or can mount S3 Files at `/mnt/yinshi-s3-files`.

For Archil profiles, `SharedFilesMountCommand` should install or locate the Archil CLI, authenticate using user-managed secrets, and mount the Archil disk at `/mnt/archil/yinshi`. The template intentionally does not include Archil credentials or opinionated Archil provisioning because those secrets and account choices belong to the user.

## Validation behavior

Yinshi validates the selected profile during runner registration and every heartbeat.

- Missing `storage_profile` defaults to `aws_ebs_s3_files` for prerelease runner compatibility.
- AWS BYOC rejects SQLite paths under the shared files path.
- Archil shared-files mode requires `shared_files_storage=archil` and rejects SQLite paths under the Archil shared path.
- Archil all-POSIX mode requires both `sqlite_storage=archil` and `shared_files_storage=archil`.
- A registered runner cannot switch profiles in a later heartbeat.

Invalid profiles and unsafe layouts return `400` responses. Invalid registration tokens and runner bearer tokens return `401` responses.
