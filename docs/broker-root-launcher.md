# Broker and root-launcher foundation

This foundation defines a future trusted execution boundary. It does not connect to production execution or enable any service.

## Current security state

Effectful launch execution defaults to `false` in source and service templates. Operators must keep this setting disabled until runtime qualification receives approval.

Application code owns signed logical requests and runs as `yinshi-app`. The `yinshi-broker` account uses UID 999 and owns request acceptance plus its SQLite journal. Root owns containment setup. A fixed pool of 32 accounts, named `yinshi-executor-00` through `yinshi-executor-31`, owns private runtime files. Each account must have a unique UID and primary GID.

Broker requests use bounded canonical JSON and Ed25519 signatures. Each request binds both incarnations and its sequence. It also binds the operation ID and request type. The signature covers the nonce and payload digest. Unix peer credentials provide a separate identity check. Broker responses carry matching identity fields and a broker signature.

## Artifact upload transport

Large replica artifacts use a separate socket at `/run/yinshi/artifact-upload.sock`. The existing broker socket and `yinshi-broker-stdio-relay` remain control-frame transports only. `yinshi-broker-artifact-stdio-relay` forwards one bounded signed control frame, then opaque artifact bytes, to the fixed upload socket. It accepts no caller-selected path.

The upload body contains exactly `committed.bundle`, `worktree.yra`, and `index-objects.pack` in that order. Signed metadata declares each digest and length. It also binds repository, workspace, authority, object format, source state, reconciliation fingerprint, configured limits, and the complete artifact-set digest. Kernel peer credentials and the Ed25519 request signature are checked before artifact bytes are read.

The daemon streams bounded chunks into `/var/lib/yinshi/artifact-incoming/<operation-id>`. It uses private pending directories and atomic no-replace publication. Exact retries consume and verify the complete body before they reuse the deterministic receipt. Rejected attempts remain quarantined when cleanup certainty requires retention. Artifact bytes never enter SQLite.

`yinshi-artifact-upload` reads only `/etc/yinshi/artifact-upload.json`. The configuration contains the application UID, fixed key paths, both incarnations, the fixed artifact root, and the exact configured limits digest. Its connection deadline exceeds the control and transfer deadlines. Its service is separate from `yinshi-broker.service`.

## Journal behavior

New requests use `/var/lib/yinshi/control/broker-journal-v2.sqlite3`. The version 1 journal remains read-only and receives no migration writes. Broker startup checks version 1 before it creates version 2 or accepts a connection. An absent version 1 file is valid. Startup stops for unreadable data, invalid schema, unsupported versions, accepted operations, or unresolved WAL and SHM files.

The version 2 journal binds one application identifier to schema version 2. Each open checks SQLite integrity, schema objects, metadata, event order, and stored frame encoding.

A new request first records accepted intent. One process can then append an immutable lifecycle claim. That owner records `prepare`, `runtime_setup`, and `launch` in this fixed order. Every external effect requires a durable stage-start event. Each completed stage requires a durable outcome before the next stage starts.

Only the claim owner can contact the launcher. Concurrent retries wait briefly for its stored result and never repeat an effect. A retry receives only a stored terminal or unresolved response.

Confirmed launch outcomes are `launched` or `rejected`. Transport, timeout, cancellation, verification, and unknown commit outcomes remain unresolved at their exact stage. They never become confirmed failure or stop records. Launcher client deadlines use `timeout_unknown`. Transport loss uses `transport_unknown`. Invalid authenticated responses use `verification_unknown`.

Records are append-only. Exact retries return the stored signed response. Invalid requests cause no launcher call. Journal synchronization failures also cause no launcher call. No SQLite handle remains open across a launcher call or runtime effect.

Each append uses an exact-event commit check after an uncertain SQLite result. The journal closes the failed connection before it checks with a new connection. A matching event continues as committed. A missing pre-effect event stops before execution. A missing post-effect outcome becomes `stage_unresolved` with reason `sqlite_commit_unknown`. The broker writes that state and its signed response together. It sends no response if this write cannot be verified.

The broker creates a private operation directory before launch. It creates and retains an AF_UNIX listener named `session.sock` with mode `0600`. Root later performs the approved protected-pin transition for executor access. A successful reply follows only after the confirmed launch response is durable.

## Fixed launcher profile

The launcher accepts one canonical request format from UID 999. The action must be `launch`, `prepare`, or `reclaim`.

The launcher daemon sends root actions to one persistent child process over a private bounded socket pair. That child owns a separate process group and handles one request at a time. The parent event loop remains responsive during root work. A deadline or canceled request kills the process group and reaps the child. The parent sends no late reply and rejects further effects until daemon restart. Restart validation must resolve any possible systemd handoff before reuse.

```json
{"action":"launch","operation_id":"0123456789abcdef0123456789abcdef","protocol_version":"yinshi-root-launcher-v1"}
```

The operation ID contains 32 lowercase hexadecimal characters. Requests cannot supply commands, paths, environments, identities, mounts, sockets, cgroups, units, or systemd properties. Each launcher response echoes the exact action and operation ID.

Before systemd contact, the launcher checks account mappings and every directory ancestor. It also checks ownership, modes, object types, link counts, socket identity, and unit absence. The launcher hard-links the accepted socket below a root-owned pin name. Directory descriptors remain open until systemd accepts the fixed command.

| Derived item | Fixed location |
| --- | --- |
| Broker staging | `/var/lib/yinshi-launcher/staging/<operation-id>` |
| Replica | `/var/lib/yinshi-launcher/replicas/<operation-id>/repo` |
| Runtime home | `/var/lib/yinshi-launcher/replicas/<operation-id>/home` |
| Runtime socket | `/run/yinshi-launcher-state/workloads/<operation-id>/session.sock` |
| Protected socket pin | `/run/yinshi-launcher-state/pins/<operation-id>` |
| Unit | `yinshi-executor-<operation-id>.service` |
| Cgroup | `/sys/fs/cgroup/yinshi-workloads.slice/<unit>` |
| Quarantine root | `/var/lib/yinshi-launcher/quarantine/<operation-id>` |
| Executable | `/opt/yinshi/runtime-v1/bin/yinshi-executor` |

The launcher selects the lowest free executor account. Prepared `repo` and `home` ownership persists this selection. The generated unit runs as that account. It uses a private network, private temporary directory, and protected system paths. `NoNewPrivileges` applies. Only AF_UNIX is available. Capability bounds are empty.

| Resource | Limit |
| --- | ---: |
| VM CPU | 1 |
| VM memory | 1,024 MiB |
| VM disk | 8 GiB |
| Executor CPU quota | 1 CPU |
| Executor memory | 640 MiB |
| Executor tasks | 128 |

`deploy/orbstack/yinshi-runtime.profile.json` records the disabled VM profile. Qualification must compare live OrbStack settings with this file.

## Installation preparation

Do not activate these files on a production host. An approved qualification procedure must verify every account mapping before installation.

Reviewed executables belong below root-owned `/opt/yinshi/runtime-v1/bin` with mode `0755`. Systemd units belong in `/etc/systemd/system` with root ownership and mode `0644`.

OrbStack adds `/run/systemd/system/service.d/zzz-lxc-service.conf`. This global override disables several namespace and filesystem controls. Install each reviewed `zzzz-yinshi-hardening.conf` in its matching unit-specific `.service.d` directory. After each daemon starts, inspect the effective systemd properties. Refuse qualification if any reviewed control is inactive.

`yinshi-broker` owns `/var/lib/yinshi`, which contains broker control and journal data. Root owns `/var/lib/yinshi-launcher` with mode `0711`. Its broker-owned `staging` child uses mode `0700`. Root owns its `replicas` child with mode `0711` and its `quarantine` child with mode `0700`. Directory definitions come from `deploy/tmpfiles.d/yinshi.conf`.

Root owns `/run/yinshi-launcher-state` with mode `0711`. Its `workloads` child belongs to `yinshi-broker:yinshi-broker` with mode `0711`. Broker-owned operation children use mode `0700`. The broker creates each socket as `yinshi-broker:yinshi-broker` with mode `0600`. Root first links the exact socket to a temporary name below the protected `pins` child. Root changes metadata through that protected name. Root then rechecks source identity and renames the temporary name to the final pin. Failure cleanup removes temporary protected state and syncs the pin parent. `/run/yinshi-launcher` belongs to `root:yinshi-broker` with mode `0710`.

Each service group name must resolve to its matching user primary GID. Broker and application accounts must not belong to any executor primary group. Executor accounts must not belong to another protected primary group. Broker key storage must exclude every executor account. Application private keys must also remain outside broker storage and executor-visible storage.

All privileged effects remain disabled when these settings are false: `YINSHI_LAUNCH_EXECUTION_ENABLED`, `YINSHI_REPLICA_PREPARE_ENABLED`, `YINSHI_REPLICA_RECLAIM_ENABLED`, and `YINSHI_REPLICA_UPLOAD_ENABLED`. Do not enable any socket. Units omit an `[Install]` section. An approved qualification run may start the sockets while all effects remain disabled.

## Replica prepare and reclaim actions

Prepare transfers one broker-staged replica tree to a protected prepared namespace. It first requires an absent unit, absent runtime, and empty cgroup.

Under one launcher lock, prepare indexes all prepared operations and chooses the lowest free executor slot. Duplicate active slot markers block every root action. A retry derives one existing slot from `repo` and `home` ownership and rejects conflicting markers. Quarantine does not reserve a slot. Pool exhaustion fails before promotion. Root atomically moves the operation from `staging` to `replicas`. Root then owns the operation directory with mode `0700`. This ownership blocks broker topology changes.

The launcher retains `O_NOFOLLOW` directory descriptors and compares Linux mount IDs. It rejects mount crossings, hard-linked regular files, special files, foreign owners, and replaced entries.

The launcher preserves repository symbolic links without following them. After promotion, it writes the selected executor name to a root-owned temporary marker. It syncs the file, renames it to `.executor-identity`, and syncs the operation directory. It then assigns `repo` and `home` to the selected UID and GID before recursive transfer. A restart can finish interrupted marker publication before ownership transfer. It transfers each child before its directory and keeps both top directories at mode `0700`.

The launcher syncs the destination parent before the source parent after promotion. It also syncs transferred metadata. A retry can finish an interrupted transfer in the protected prepared namespace without changing slots.

Launch validates the same prepared-state invariant. It pins the accepted socket inode below `/run/yinshi-launcher-state/pins`. Each workload receives a private temporary `/run` filesystem. Systemd binds only the protected pin at `/run/session.sock` in that private mount. The executor receives that path through `YINSHI_SESSION_SOCKET`. Other workloads cannot see this mount. Broker pathname replacement cannot change the accepted inode.

Reclaim validates the selected identity, then moves one stopped prepared operation into the root-owned quarantine. The launcher first requires a confirmed absent unit and an absent runtime directory. Reclaim does not trust mode bits on executor-owned top directories. Launch still requires mode `0700`. Quarantine ownership does not reserve a pool slot.

It reads `cgroup.events` and requires `populated 0` for an existing cgroup. Removal does not follow links or cross Linux mount boundaries.

Reclaim removes and syncs a valid final or temporary socket pin after quiescence checks. It does this before it deletes executor-controlled content. Iterative descriptor traversal supports deep trees. The root-owned operation marker remains as a small quarantine tombstone. It preserves retry identity without reserving the executor slot. A foreign or malformed pin blocks removal and remains available for inspection.

An absent prepared and quarantine target produces an idempotent success response. Other failures retain retryable partial state.

## Qualification before integration

Qualification must inspect effective systemd properties, cgroup limits, peer credentials, ownership, mount visibility, restart recovery, and Linux failure behavior. It must test an authenticated request through both daemons. Disabled prepare must return `prepare_disabled`. The journal must retain accepted intent, lifecycle ownership, and the confirmed prepare rejection. A reboot must leave both sockets inactive. A manual restart must replay the stored response without another launcher contact.
