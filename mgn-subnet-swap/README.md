# AWS MGN launch template subnet swap

`scripts/mgn_subnet_swap.py` reviews AWS Application Migration Service (MGN)
source servers, reads each server's configured EC2 launch template, and replaces
matching network-interface subnet IDs in the default launch template version.

The script is dry-run by default. It only creates new launch template versions and
sets them as default when `--execute` is supplied.

## Execution plan

1. Enumerate MGN source servers with `DescribeSourceServers`.
2. Exclude archived servers unless `--include-archived` is supplied.
3. Apply optional criteria: MGN application ID, source server ID regex, server
   name regex, launch template ID regex, and exact tags.
4. For each matched server, call `GetLaunchConfiguration` to find
   `ec2LaunchTemplateID`.
5. Read the EC2 launch template default version.
6. Clone the launch template network-interface block and change only
   `NetworkInterfaces[*].SubnetId` values equal to `--from-subnet`.
7. In dry-run mode, report the intended changes.
8. In execute mode, create a new launch template version from the current default
   version and set the new version as the launch template default.

## Adversarial review and mitigations

- Ambiguous criteria could update too many servers. The script requires
  `--from-subnet` and only edits templates containing that exact subnet. Use tag,
  application, name, source server, or launch-template filters to narrow further.
- Archived source servers are easy to change accidentally. They are skipped by
  default.
- Launch templates can contain multiple network interfaces. The script changes
  every network-interface subnet field that exactly matches `--from-subnet` and
  reports each path.
- Some launch templates may not specify a subnet. Those are skipped, not inferred.
- Rebuilding template data can drop settings. The script creates the new version
  from the existing default version and submits only the modified network
  interface list, so other launch template fields are inherited unchanged.
- Setting the new version as default changes future MGN test/cutover launches.
  That write path requires `--execute`.

## Examples

Dry-run every non-archived source server whose default launch template references
`subnet-0123456789abcdef0`:

```powershell
py scripts/mgn_subnet_swap.py `
  --profile my-profile `
  --region us-east-1 `
  --from-subnet subnet-0123456789abcdef0 `
  --to-subnet subnet-0fedcba9876543210
```

Execute only for servers tagged `MigrationWave=wave-4`:

```powershell
py scripts/mgn_subnet_swap.py `
  --profile my-profile `
  --region us-east-1 `
  --from-subnet subnet-0123456789abcdef0 `
  --to-subnet subnet-0fedcba9876543210 `
  --tag MigrationWave=wave-4 `
  --execute
```

Emit JSON for audit or review:

```powershell
py scripts/mgn_subnet_swap.py `
  --region us-east-1 `
  --from-subnet subnet-0123456789abcdef0 `
  --to-subnet subnet-0fedcba9876543210 `
  --output json
```

## Required IAM permissions

The caller needs read permissions for MGN and launch templates:

- `mgn:DescribeSourceServers`
- `mgn:GetLaunchConfiguration`
- `ec2:DescribeLaunchTemplateVersions`

Execute mode also needs:

- `ec2:CreateLaunchTemplateVersion`
- `ec2:ModifyLaunchTemplate`

## Notes

- The script updates EC2 launch template defaults. It does not start test or
  cutover launches.
- It targets subnet IDs configured under
  `LaunchTemplateData.NetworkInterfaces[*].SubnetId`.
- Run without `--execute` first and review the output before applying changes.

# AWS MGN READY_FOR_TEST to READY_FOR_CUTOVER

`scripts/mgn_ready_for_cutover.py` reviews AWS MGN source servers and changes
servers whose lifecycle state is `READY_FOR_TEST` to `READY_FOR_CUTOVER`.

The script is dry-run by default. It only calls `ChangeServerLifeCycleState` when
`--execute` is supplied.

## Execution plan

1. Enumerate MGN source servers with `DescribeSourceServers`.
2. Exclude archived servers unless `--include-archived` is supplied.
3. Apply optional criteria: MGN application ID, source server ID regex, server
   name regex, and exact tags.
4. Report servers already in any lifecycle state other than `READY_FOR_TEST` as
   skipped.
5. In dry-run mode, report each server that would move to `READY_FOR_CUTOVER`.
6. In execute mode, call `ChangeServerLifeCycleState` for each matched
   `READY_FOR_TEST` server.

## Examples

Dry-run every non-archived source server:

```powershell
py scripts/mgn_ready_for_cutover.py `
  --profile my-profile `
  --region us-east-1
```

Execute only for servers tagged `MigrationWave=wave-4`:

```powershell
py scripts/mgn_ready_for_cutover.py `
  --profile my-profile `
  --region us-east-1 `
  --tag MigrationWave=wave-4 `
  --execute
```

Emit JSON for audit or review:

```powershell
py scripts/mgn_ready_for_cutover.py `
  --region us-east-1 `
  --output json
```

## Required IAM permissions

The caller needs read permissions for MGN:

- `mgn:DescribeSourceServers`

Execute mode also needs:

- `mgn:ChangeServerLifeCycleState`
