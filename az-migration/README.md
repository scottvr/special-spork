# EC2 Availability Zone Migration Tool

Single Python CLI for migrating an EC2 instance to another Availability Zone in the same AWS region.

The supported entry point is:

```bash
python scripts/ec2-migrate.py <command> [options]
```

## What It Does

The migration flow is intentionally conservative:

1. Validate the source instance exists, is not terminated, and is not already in the target AZ.
2. Record the source instance configuration, attached EBS volumes, security groups, subnet, tags, and IAM instance profile.
3. Create an AMI from the source instance, tag it to match the source instance, and wait for the AMI to become available.
4. Select a target subnet in the same VPC and requested target AZ, or validate the subnet you provide.
5. Launch a replacement instance from the migration AMI with the same instance type, key pair, security groups, IAM instance profile, EBS optimization, monitoring setting, metadata options where applicable, and source instance tags in the creation API call.
6. Synchronize user-managed tags so corresponding resources match:
   - source instance tags to the replacement instance and migration AMI
   - source EBS volume tags to replacement EBS volumes by device name
   - source EBS volume tags to AMI snapshots by device name
   - source network interface tags to replacement network interfaces by device index
7. Wait for the replacement instance to reach `running` and write a timestamped migration log under `logs/`.

The source instance is not stopped or terminated by `migrate`. Cleanup is a separate explicit command.

AWS-reserved tags with keys beginning with `aws:` cannot be created or deleted, so the tool ignores those during tag synchronization. For user-managed tags, the tool adds missing tags, updates changed values, and removes extra tags from the target resource.

## Requirements

- Python 3.8+
- AWS credentials configured through environment variables, instance profile, or an AWS profile
- Python dependencies from `requirements.txt`
- IAM permissions for the EC2 and optional Route53 actions you use

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Commands

### Migrate

```bash
python scripts/ec2-migrate.py --profile default --region us-east-1 migrate \
  --instance-id i-1234567890abcdef0 \
  --target-az us-east-1b
```

Use a specific subnet:

```bash
python scripts/ec2-migrate.py migrate \
  --instance-id i-1234567890abcdef0 \
  --target-az us-east-1b \
  --target-subnet subnet-1234567890abcdef0
```

Validate AWS permissions without creating resources:

```bash
python scripts/ec2-migrate.py migrate \
  --instance-id i-1234567890abcdef0 \
  --target-az us-east-1b \
  --dry-run
```

By default AWS may reboot the instance while creating the AMI to preserve file-system consistency. To skip that reboot:

```bash
python scripts/ec2-migrate.py migrate \
  --instance-id i-1234567890abcdef0 \
  --target-az us-east-1b \
  --no-reboot
```

### Validate

Run this after migration using the replacement instance ID printed in the migration log:

```bash
python scripts/ec2-migrate.py validate --instance-id i-newinstanceid
```

Validation checks the instance state, status checks when available, network identity, subnet, and security groups.

### Update DNS

Update a Route53 A record to point to the replacement instance private or public IP:

```bash
python scripts/ec2-migrate.py update-dns \
  --hosted-zone example.com \
  --record-name app.example.com \
  --instance-id i-newinstanceid \
  --ip-type private
```

Preview the Route53 change batch without submitting it:

```bash
python scripts/ec2-migrate.py update-dns \
  --hosted-zone example.com \
  --record-name app.example.com \
  --instance-id i-newinstanceid \
  --dry-run
```

### Cleanup

Inventory migration-related resources:

```bash
python scripts/ec2-migrate.py cleanup --instance-id i-sourceinstanceid
```

Deregister a migration AMI:

```bash
python scripts/ec2-migrate.py cleanup \
  --instance-id i-sourceinstanceid \
  --ami-id ami-1234567890abcdef0
```

Deregister the AMI and delete snapshots created for that AMI:

```bash
python scripts/ec2-migrate.py cleanup \
  --instance-id i-sourceinstanceid \
  --ami-id ami-1234567890abcdef0 \
  --delete-snapshots
```

Stop or terminate the source instance only after you have confirmed the replacement instance is healthy:

```bash
python scripts/ec2-migrate.py cleanup --instance-id i-sourceinstanceid --stop-instance
python scripts/ec2-migrate.py cleanup --instance-id i-sourceinstanceid --terminate-instance
```

Use `--dry-run` with cleanup to log intended actions without performing them.

## Logs

Logs are written to `logs/`:

- `migration_YYYYMMDD_HHMMSS.log`
- `validation_YYYYMMDD_HHMMSS.log`
- `dns_update_YYYYMMDD_HHMMSS.log`
- `cleanup_YYYYMMDD_HHMMSS.log`

The migration log contains the new instance ID and AMI ID needed for follow-up validation, DNS updates, and cleanup.

## IAM Permissions

Minimum EC2 permissions for migration and validation:

```json
{
  "Effect": "Allow",
  "Action": [
    "ec2:CreateImage",
    "ec2:CreateTags",
    "ec2:DeleteTags",
    "ec2:DescribeImages",
    "ec2:DescribeInstanceStatus",
    "ec2:DescribeInstances",
    "ec2:DescribeSubnets",
    "ec2:DescribeTags",
    "ec2:DescribeVolumes",
    "ec2:RunInstances"
  ],
  "Resource": "*"
}
```

Add these for cleanup:

```json
{
  "Effect": "Allow",
  "Action": [
    "ec2:DeleteSnapshot",
    "ec2:DeregisterImage",
    "ec2:StopInstances",
    "ec2:TerminateInstances"
  ],
  "Resource": "*"
}
```

Add these for Route53 DNS updates:

```json
{
  "Effect": "Allow",
  "Action": [
    "route53:ChangeResourceRecordSets",
    "route53:ListHostedZonesByName",
    "route53:ListResourceRecordSets"
  ],
  "Resource": "*"
}
```

If the source instance has an IAM instance profile, the caller may also need `iam:PassRole` for the role in that profile.

## Operational Notes

Run a dry run first, schedule production migrations in a maintenance window, and keep the source instance available until the replacement instance has passed application-level checks. This tool validates AWS infrastructure state; it cannot verify your application-specific health unless you add that check outside the tool.
