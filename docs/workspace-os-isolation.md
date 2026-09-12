# Workspace OS isolation

## Status and scope

The A4 module is a pure offline reconciliation core. It validates immutable facts, derives one deterministic protocol, and classifies recovery observations. It does not inspect a host or perform an effect.

This component has no runtime wiring. It does not configure accounts, services, permissions, credentials, or OS isolation. Its presence does not establish Phase 6 isolation.

## Bound specification

`bind_specification` accepts one complete version 1 `ReconciliationSpec`. Exact frozen dataclass types are required. Variable collections use tuples, and binary values use immutable `bytes`. Boolean and integer fields reject values of other exact types.

`Location` identifies a namespace plus a raw byte path. `NodeIdentity` separately identifies a filesystem and one node on that filesystem. A physical target ID never substitutes for either identity.

The specification binds the operation identity, concrete baseline, desired state, protected boundaries, private preparation slots, quarantine slots, marker templates, object inventories, and limits. Baseline nodes always have concrete identities. Desired nodes use either `ExistingNode(identity)` or `PreparedNode(slot_id)`. They never claim a future node identity.

Every prepared reference must have one declared producer. Every quarantine move must preserve the source identity and remain on the expected filesystem. Replacement of a directory first quarantines all descendants. Unchanged descendants return with their existing identities after the new directory is installed.

The generation contract is exact. Baseline generation equals the operation generation. Desired generation is baseline generation plus one. The drained replica generation remains fixed. Reopening a replica requires a new operation with new matching facts.

## Paths, topology, and links

Raw paths preserve non-UTF-8 bytes. Validation rejects absolute paths, empty components, dot components, NUL bytes, and unsupported node kinds. Regular files and symbolic links cannot have multiple hard links. Privilege bits are not supported.

A manifest is complete. Each non-root entry has every parent entry, and each parent is a directory. No descendant can appear below a file or a symbolic link. The index independently rejects prefix collisions such as `a` with `a/b`.

A prepared parent resolves to one consumed directory preparation at the exact parent location. The parent installation must precede every dependent installation or restoration. Self-reference, cycles, missing targets, wrong targets, and file or symbolic-link parent preparations fail closed. A newly installed directory starts with link count two. Each direct prepared child directory increments that count in its later postimage.

Symbolic-link targets are opaque bytes. Reconciliation compares and transfers those bytes without traversing the link. This rule does not prove that a link target stays inside the workspace. Runtime containment requires separately configured OS isolation.

## Administrative state

Index storage has an explicit `AbsentState`. A missing index differs from a present empty index file. Semantic index entries remain independent from working-tree content.

Selected-ref state records the selected name, HEAD binding, resolved object ID, and optional packed fallback. It also records concrete HEAD storage, loose-ref storage, packed refs, reflogs, and unrelated metadata. Desired administrative storage uses existing or prepared references. Exact absence, content, mode, and identity remain distinct.

All administrative storage uses the `admin` namespace. Working storage uses the `working` namespace. Each physical location has one semantic registration in each static phase. Reuse across phases requires the same logical field or an explicit quarantine before installation.

Present administrative storage and durable object storage must use regular, non-executable, single-link files. Acknowledgment actions assert that physical state already matches the desired state. They cannot assign desired physical state. Generation publication changes only the generation field.

## Inventories and limits

Required objects form a bounded semantic inventory. `RequiredObject.byte_length` is the length of the uncompressed Git object payload only. `RequiredObject.content_sha256` is the lowercase SHA-256 digest of that payload, prefixed with `sha256:`.

`RequiredObject.object_id` uses the selected Git object format. Its digest input is the object kind in ASCII, one space, the decimal payload length without leading zeros, one NUL byte, and the uncompressed payload. The allowed kinds are `blob`, `tree`, `commit`, and `tag`.

The external inventory producer validates the relationship among object ID, kind, byte length, and payload digest. `InventoryFact` binds that assertion to the operation and specification. This core validates syntax, exact supplied values, coverage, owner, receipt, operation, and specification binding. Git parsing and traversal stay outside this module.

Known semantic blob vectors are:

| Payload | Payload length | Payload SHA-256 | SHA-1 object ID | SHA-256 object ID |
| --- | ---: | --- | --- | --- |
| empty bytes | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391` | `473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813` |
| `b"test content\n"` | 13 | `a1fff0ffefb9eace7230c24e50731f0a91c62f9cefdfe77121c2f607125dffae` | `d670460b4b4aece5915caf5c68d12f560a9fe3e4` | `13b7e821533d3fe3728a3c4560606a65aab99f4390b9df0714f9075c0ef4c2d6` |

Destination objects form a separate durable physical inventory. `DestinationObject.content_sha256` covers the exact bytes in `DestinationObject.storage.content`. The storage can contain compressed loose-object bytes. These bytes do not need to equal the semantic payload bytes or payload digest.

For example, these two zlib streams both encode the SHA-1 blob `d670460b4b4aece5915caf5c68d12f560a9fe3e4`:

| Exact physical bytes in hexadecimal | Physical SHA-256 |
| --- | --- |
| `78014bcac94f5230346628492d2e5148cecf2b49cd2be102004bdf0709` | `7a254758b549802a2e0681b1d15208fc42d4754dc916c24e16790cecdd3cc0bf` |
| `78da4bcac94f5230346628492d2e5148cecf2b49cd2be102004bdf0709` | `6459ff11c60f0de91f1a8c080a9cebce28678df1199d95314b9c4aca68e688f5` |

Existing object identities and durability receipts are bound inputs. Planning promotes every missing required object before index or ref mutation. Promotion is no-replace and rejects conflicting destination identity.

Limits bound paths, objects, total bytes, path depth, and expanded transitions. `max_paths` counts every path-bearing schema field, including repeated fields. It covers replica storage, manifests, index entries, selected refs, boundaries, parents, preparations, quarantine state, markers, and absence observations. It also covers derived required-object destinations. `max_depth` applies to the same complete collection.

`max_total_bytes` bounds the canonical specification projection byte length plus the sum of all semantic `RequiredObject.byte_length` values. Binding applies path count, depth, decoded-byte, and structural traversal guards before expensive topology checks and canonical projection. Structural traversal stops at 256 levels. Protocol integers cannot exceed the signed 64-bit maximum. `BoundSpecification` stores the exact compiled transition count. Planning recompiles the same normalized transition inventory and requires that count to match before deriving images.

## Fingerprints and plan authority

The specification fingerprint is:

```text
"sha256:" + SHA256(
    b"yinshi.workspace-replica.spec\x00v1\x00" + canonical_projection
).hexdigest()
```

The projection includes every specification field. It excludes derived actions, derived marker bytes, later journal facts, and fresh observations. Marker templates enter the projection. Fingerprint-bearing marker payloads are derived only after binding, which avoids self-reference.

Canonical projection uses compact UTF-8 JSON with sorted keys. Bytes use padded standard base64. Tagged variants preserve absence separately from empty content. The core sorts unordered collections by typed canonical keys. Ordered ancestor chains keep structural order. The core rejects duplicate keys before sorting.

`BoundSpecification` also exposes domain-separated baseline and desired fingerprints. `observation_fingerprint` binds each complete concrete fresh observation. Completion facts use this observation fingerprint.

Plans have no independent authority. Planning and replay derive every action again from the validated bound specification. An optional cached plan must equal the derived plan exactly. Truncation, reordering, duplication, weakened synchronization, and disconnected images therefore fail closed.

`ReconciliationPlan.content_mutation_count` is read-only derived information. It counts canonical object promotion and concrete quarantine, install, or restore actions. It does not authorize work or remove protocol barriers.

## Ownership and retained facts

The application owns logical reservation and destination binding. The broker owns physical effects, durability, generation publication, and physical release. The core owns permitted state transitions. A future adapter may perform requested effects, but it cannot invent transition rules.

Retained acknowledgment, drain, export, and inventory facts keep their original owners and receipt IDs. They are immutable journal inputs and are revalidated during replay. Supplying a fact is not authentication. Credential validation and authorization remain adapter responsibilities outside this offline core.

A `ReconciliationJournal` stores retained inputs plus separate intent facts, preparation facts, synchronization facts, and completion facts. Each protocol fact carries the bound fingerprint. A completion records the exact expected observation fingerprint. Preparation facts record the observed node identity because desired state cannot predict it. Preparation and synchronization facts carry required `JournalPosition` values from one authenticated append-only journal. The operation ID identifies that journal. Each positive sequence and append receipt is unique. Callers cannot assign or revise these values. The broker adapter must authenticate each append receipt before it creates the offline fact. Creation synchronization must identify the matching creation receipt and have a later position. A stale pre-effect fact remains invalid after preparation appears. Older facts without authenticated positions require explicit migration and cannot enter reconciliation.

## Sixteen protocol stages

| Stage | Required transition |
| --- | --- |
| 1 | Commit the application intent and exact reservation. |
| 2 | Record broker acceptance, create each exclusion marker, and synchronize each marker parent. |
| 3 | Persistently close the admission gate, drain readers and writers, and retain drain completion. |
| 4 | Validate the original acknowledgment, export, replica generation, and inventory. |
| 5 | Compare the complete baseline and protected identities. |
| 6 | For each preparation, retain intent, materialization identity, content synchronization, parent synchronization, and completion. |
| 7 | Promote each required object and complete its required synchronization. |
| 8 | Quarantine each working node deepest-first and synchronize each changed parent. |
| 9 | Install prepared nodes and restore retained nodes parent-first, then synchronize each changed parent. |
| 10 | Quarantine the index, preserve explicit absence, install any replacement, and synchronize changed parents. |
| 11 | Reconcile metadata and reflogs, quarantine the loose ref, apply packed fallback, and install the selected ref last. |
| 12 | Verify complete desired state, quarantine state, object durability, and protected boundaries. |
| 13 | Publish the broker generation. |
| 14 | Bind the application destination. |
| 15 | Record broker acknowledgment of the destination binding. |
| 16 | Record release intent, remove and synchronize each marker, release physically, release logically, and admit readers. |

No-op content reconciliation still crosses all required control barriers. Marker creation and removal remain explicit. Physical release cannot imply logical release. Reader admission occurs only after both releases are durable.

## Replay and crash behavior

Replay first rebinds the specification and derives the authoritative plan. It validates retained facts and requires journal completions to form one exact prefix. Future intent, synchronization, or completion facts are rejected.

Every action has a durable intent boundary, permitted effect postimages, required synchronization, and an exact completion postimage. Replay always uses a new `FreshObservation`. It never treats a retained observation as current host state.

If fresh state still matches the preimage after intent, replay requests the effect. If fresh state matches an allowed effect postimage, replay does not repeat the effect. It requests only missing synchronization. Completion becomes recordable only after every required synchronization fact exists.

Prepared identity is an exception that fails closed. If creation changed fresh state but its node identity was not retained, replay reports `lost_preparation_identity`. Replay also refuses when a retained creation fact exists but the fresh destination is absent. It cannot safely repeat creation or infer that identity.

A foreign identity, unexpected boundary, impossible prefix, or unrecognized intermediate image stops replay. A restarted process can continue only from valid retained facts plus a fresh exact observation. A reopened replica cannot reuse the old drain or export generation.

## Integration boundary

A future adapter must get authoritative external facts, authenticate their sources, perform each requested effect, and persist receipts durably. It must return complete fresh observations at every recovery call. Those requirements are outside this offline change.

No schema, service wiring, provisioning, runtime effect, dependency, or deployment change is included here.
