# Quick Start

## Install

```bash
python -m pip install -r requirements.txt
```

## Dry Run

```bash
python scripts/ec2-migrate.py --profile default --region us-east-1 migrate \
  --instance-id i-1234567890abcdef0 \
  --target-az us-east-1b \
  --dry-run
```

## Migrate

```bash
python scripts/ec2-migrate.py --profile default --region us-east-1 migrate \
  --instance-id i-1234567890abcdef0 \
  --target-az us-east-1b
```

Watch the console output or latest `logs/migration_*.log` for:

```text
AMI created: ami-...
New instance launched: i-...
```

## Validate

```bash
python scripts/ec2-migrate.py --profile default --region us-east-1 validate \
  --instance-id i-newinstanceid
```

## Optional DNS Cutover

```bash
python scripts/ec2-migrate.py --profile default --region us-east-1 update-dns \
  --hosted-zone example.com \
  --record-name app.example.com \
  --instance-id i-newinstanceid \
  --ip-type private
```

## Optional Cleanup

Inventory first:

```bash
python scripts/ec2-migrate.py --profile default --region us-east-1 cleanup \
  --instance-id i-sourceinstanceid
```

After application validation, deregister the migration AMI:

```bash
python scripts/ec2-migrate.py --profile default --region us-east-1 cleanup \
  --instance-id i-sourceinstanceid \
  --ami-id ami-1234567890abcdef0
```

Stop the source instance only after the replacement is confirmed healthy:

```bash
python scripts/ec2-migrate.py --profile default --region us-east-1 cleanup \
  --instance-id i-sourceinstanceid \
  --stop-instance
```

The full migration flow is documented in [README.md](README.md).
