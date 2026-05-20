#!/usr/bin/env python3
"""EC2 Availability Zone migration utility."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from botocore.exceptions import ClientError
except (
    ImportError
):  # pragma: no cover - allows --help before dependencies are installed.
    ClientError = Exception  # type: ignore[misc,assignment]


REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"


def configure_logging(prefix: str) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
        force=True,
    )
    return log_file


def aws_session(profile: str | None, region: str | None) -> boto3.Session:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency boto3. Install dependencies with: python -m pip install -r requirements.txt"
        ) from exc

    kwargs: dict[str, str] = {}
    if profile:
        kwargs["profile_name"] = profile
    if region:
        kwargs["region_name"] = region
    return boto3.Session(**kwargs)


def get_instance(ec2: Any, instance_id: str) -> dict[str, Any]:
    response = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = response.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        raise ValueError(f"Instance {instance_id} was not found")
    return reservations[0]["Instances"][0]


def clean_name(name: str) -> str:
    return name.rstrip(".")


def user_tags(tags: list[dict[str, str]] | None) -> list[dict[str, str]]:
    return [tag for tag in tags or [] if not tag["Key"].lower().startswith("aws:")]


@dataclass
class MigrationResult:
    source_instance_id: str
    target_az: str
    ami_id: str | None = None
    new_instance_id: str | None = None
    log_file: Path | None = None


class EC2Migrator:
    def __init__(
        self,
        *,
        instance_id: str,
        target_az: str,
        target_subnet: str | None,
        profile: str | None,
        region: str | None,
        dry_run: bool,
        no_reboot: bool,
    ) -> None:
        session = aws_session(profile, region)
        self.ec2 = session.client("ec2")
        self.instance_id = instance_id
        self.target_az = target_az
        self.target_subnet = target_subnet
        self.dry_run = dry_run
        self.no_reboot = no_reboot
        self.source_instance: dict[str, Any] | None = None
        self.source_userdata: str | None = None
        self.source_termination_protection: bool = False

    def run(self) -> MigrationResult:
        logging.info(
            "Starting migration for %s to %s", self.instance_id, self.target_az
        )
        source = self.validate_source()
        self.log_source_details(source)
        self.log_volume_details(source)
        ami_id = self.create_ami(source)
        new_instance_id = self.launch_target_instance(source, ami_id)
        self._apply_termination_protection(new_instance_id)
        self.sync_related_tags(source, new_instance_id, ami_id)
        self.describe_new_instance(new_instance_id)
        logging.info("Migration completed")
        logging.info("Source instance: %s", self.instance_id)
        logging.info("AMI created: %s", ami_id)
        logging.info("New instance: %s", new_instance_id)
        return MigrationResult(
            self.instance_id, self.target_az, ami_id, new_instance_id
        )

    def validate_source(self) -> dict[str, Any]:
        logging.info("Validating source instance")
        source = get_instance(self.ec2, self.instance_id)
        source_az = source["Placement"]["AvailabilityZone"]
        state = source["State"]["Name"]
        if source_az == self.target_az:
            raise ValueError(
                f"Source instance is already in target AZ {self.target_az}"
            )
        if state in {"shutting-down", "terminated"}:
            raise ValueError(f"Source instance is in unsupported state {state}")
        self.source_instance = source
        self._fetch_extended_attributes()
        logging.info("Source instance found in %s with state %s", source_az, state)
        return source

    def _fetch_extended_attributes(self) -> None:
        userdata = self.ec2.describe_instance_attribute(
            InstanceId=self.instance_id, Attribute="userData"
        )
        self.source_userdata = userdata.get("UserData", {}).get("Value") or None

        termination = self.ec2.describe_instance_attribute(
            InstanceId=self.instance_id, Attribute="disableApiTermination"
        )
        self.source_termination_protection = termination.get(
            "DisableApiTermination", {}
        ).get("Value", False)

    def log_source_details(self, source: dict[str, Any]) -> None:
        logging.info("Source instance configuration")
        logging.info("  Instance type: %s", source["InstanceType"])
        logging.info("  AMI: %s", source["ImageId"])
        logging.info("  VPC: %s", source.get("VpcId", "N/A"))
        logging.info("  Subnet: %s", source.get("SubnetId", "N/A"))
        logging.info(
            "  Security groups: %s",
            ", ".join(sg["GroupId"] for sg in source.get("SecurityGroups", []))
            or "N/A",
        )
        if source.get("IamInstanceProfile"):
            logging.info(
                "  IAM instance profile: %s", source["IamInstanceProfile"]["Arn"]
            )
        logging.info("  User data: %s", "present" if self.source_userdata else "none")
        logging.info("  Termination protection: %s", self.source_termination_protection)

    def log_volume_details(self, source: dict[str, Any]) -> None:
        mappings = [m for m in source.get("BlockDeviceMappings", []) if "Ebs" in m]
        if not mappings:
            logging.info("No EBS volumes are attached")
            return
        volume_ids = [m["Ebs"]["VolumeId"] for m in mappings]
        volumes = {
            volume["VolumeId"]: volume
            for volume in self.ec2.describe_volumes(VolumeIds=volume_ids)["Volumes"]
        }
        logging.info("Source volume configuration")
        for mapping in mappings:
            volume = volumes[mapping["Ebs"]["VolumeId"]]
            logging.info(
                "  %s: %s, %s GiB, %s",
                mapping["DeviceName"],
                volume["VolumeId"],
                volume["Size"],
                volume["VolumeType"],
            )

    def create_ami(self, source: dict[str, Any]) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        ami_name = f"migration-{self.instance_id}-{timestamp}"
        image_tags = user_tags(source.get("Tags"))
        tag_specifications = []
        if image_tags:
            tag_specifications.append({"ResourceType": "image", "Tags": image_tags})
        logging.info("Creating AMI %s", ami_name)
        try:
            kwargs: dict[str, Any] = {
                "InstanceId": self.instance_id,
                "Name": ami_name,
                "Description": f"Migration AMI for {self.instance_id} to {self.target_az}",
                "NoReboot": self.no_reboot,
                "DryRun": self.dry_run,
            }
            if tag_specifications:
                kwargs["TagSpecifications"] = tag_specifications
            response = self.ec2.create_image(**kwargs)
        except ClientError as exc:
            if self.is_dry_run_success(exc):
                logging.info("Dry run passed for AMI creation")
                return "ami-dryrun"
            raise

        ami_id = response["ImageId"]
        logging.info("AMI created: %s", ami_id)
        logging.info("Waiting for AMI to become available")
        self.ec2.get_waiter("image_available").wait(ImageIds=[ami_id])
        logging.info("AMI is available")
        self.sync_resource_tags(ami_id, source.get("Tags"), "AMI")
        self.sync_ami_snapshot_tags(source, ami_id)
        return ami_id

    def target_subnet_id(self, source: dict[str, Any]) -> str:
        vpc_id = source["VpcId"]
        if self.target_subnet:
            response = self.ec2.describe_subnets(SubnetIds=[self.target_subnet])
            subnets = response.get("Subnets", [])
            if not subnets:
                raise ValueError(f"Subnet {self.target_subnet} was not found")
            subnet = subnets[0]
            if subnet["AvailabilityZone"] != self.target_az:
                raise ValueError(
                    f"Subnet {self.target_subnet} is in {subnet['AvailabilityZone']}, not {self.target_az}"
                )
            if subnet["VpcId"] != vpc_id:
                raise ValueError(
                    f"Subnet {self.target_subnet} is not in source VPC {vpc_id}"
                )
            logging.info("Using requested target subnet %s", self.target_subnet)
            return self.target_subnet

        response = self.ec2.describe_subnets(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "availability-zone", "Values": [self.target_az]},
            ]
        )
        subnets = sorted(response.get("Subnets", []), key=lambda s: s["SubnetId"])
        if not subnets:
            raise ValueError(f"No subnet found in {self.target_az} for VPC {vpc_id}")
        subnet_id = subnets[0]["SubnetId"]
        logging.info("Using target subnet %s", subnet_id)
        return subnet_id

    def launch_target_instance(self, source: dict[str, Any], ami_id: str) -> str:
        kwargs: dict[str, Any] = {
            "ImageId": source["ImageId"] if ami_id == "ami-dryrun" else ami_id,
            "MinCount": 1,
            "MaxCount": 1,
            "InstanceType": source["InstanceType"],
            "SubnetId": self.target_subnet_id(source),
            "SecurityGroupIds": [
                sg["GroupId"] for sg in source.get("SecurityGroups", [])
            ],
            "DryRun": self.dry_run or ami_id == "ami-dryrun",
        }
        instance_tags = user_tags(source.get("Tags"))
        if instance_tags:
            kwargs["TagSpecifications"] = [
                {"ResourceType": "instance", "Tags": instance_tags}
            ]
        if source.get("KeyName"):
            kwargs["KeyName"] = source["KeyName"]
        if source.get("IamInstanceProfile"):
            kwargs["IamInstanceProfile"] = {"Arn": source["IamInstanceProfile"]["Arn"]}
        if source.get("EbsOptimized") is not None:
            kwargs["EbsOptimized"] = source["EbsOptimized"]
        if source.get("Monitoring"):
            kwargs["Monitoring"] = {
                "Enabled": source["Monitoring"].get("State") == "enabled"
            }
        if source.get("MetadataOptions"):
            metadata = source["MetadataOptions"]
            kwargs["MetadataOptions"] = {
                key: metadata[key]
                for key in (
                    "HttpTokens",
                    "HttpPutResponseHopLimit",
                    "HttpEndpoint",
                    "HttpProtocolIpv6",
                    "InstanceMetadataTags",
                )
                if key in metadata
            }
        if self.source_userdata:
            kwargs["UserData"] = self.source_userdata

        logging.info("Launching replacement instance in %s", self.target_az)
        try:
            response = self.ec2.run_instances(**kwargs)
        except ClientError as exc:
            if self.is_dry_run_success(exc):
                logging.info("Dry run passed for instance launch")
                return "i-dryrun"
            raise

        new_instance_id = response["Instances"][0]["InstanceId"]
        logging.info("New instance launched: %s", new_instance_id)
        self.ec2.get_waiter("instance_running").wait(InstanceIds=[new_instance_id])
        logging.info("New instance is running")
        return new_instance_id

    def _apply_termination_protection(self, instance_id: str) -> None:
        if instance_id == "i-dryrun" or not self.source_termination_protection:
            return
        logging.info("Enabling termination protection on %s", instance_id)
        self.ec2.modify_instance_attribute(
            InstanceId=instance_id,
            DisableApiTermination={"Value": True},
        )

    def describe_resource_tags(self, resource_id: str) -> list[dict[str, str]]:
        response = self.ec2.describe_tags(
            Filters=[{"Name": "resource-id", "Values": [resource_id]}]
        )
        return [
            {"Key": tag["Key"], "Value": tag["Value"]}
            for tag in response.get("Tags", [])
        ]

    def sync_resource_tags(
        self,
        resource_id: str,
        desired_tags: list[dict[str, str]] | None,
        label: str,
    ) -> None:
        desired = {tag["Key"]: tag["Value"] for tag in user_tags(desired_tags)}
        current = {
            tag["Key"]: tag["Value"]
            for tag in user_tags(self.describe_resource_tags(resource_id))
        }

        extra_keys = sorted(key for key in current if key not in desired)
        tags_to_set = [
            {"Key": key, "Value": value}
            for key, value in sorted(desired.items())
            if current.get(key) != value
        ]

        if extra_keys:
            self.ec2.delete_tags(
                Resources=[resource_id],
                Tags=[{"Key": key} for key in extra_keys],
            )
        if tags_to_set:
            self.ec2.create_tags(Resources=[resource_id], Tags=tags_to_set)

        logging.info(
            "Synced %s tags on %s (%s set, %s removed)",
            label,
            resource_id,
            len(tags_to_set),
            len(extra_keys),
        )

    def sync_ami_snapshot_tags(self, source: dict[str, Any], ami_id: str) -> None:
        source_volumes = {
            mapping["DeviceName"]: mapping["Ebs"]["VolumeId"]
            for mapping in source.get("BlockDeviceMappings", [])
            if "Ebs" in mapping
        }
        if not source_volumes:
            return

        image = self.ec2.describe_images(ImageIds=[ami_id])["Images"][0]
        for mapping in image.get("BlockDeviceMappings", []):
            snapshot_id = mapping.get("Ebs", {}).get("SnapshotId")
            source_volume_id = source_volumes.get(mapping.get("DeviceName"))
            if not snapshot_id or not source_volume_id:
                continue
            self.sync_resource_tags(
                snapshot_id,
                self.describe_resource_tags(source_volume_id),
                f"snapshot tags from source volume {source_volume_id}",
            )

    def sync_related_tags(
        self, source: dict[str, Any], target_id: str, ami_id: str
    ) -> None:
        if target_id == "i-dryrun":
            return
        self.sync_resource_tags(target_id, source.get("Tags"), "instance")

        target = get_instance(self.ec2, target_id)
        source_volumes = {
            mapping["DeviceName"]: mapping["Ebs"]["VolumeId"]
            for mapping in source.get("BlockDeviceMappings", [])
            if "Ebs" in mapping
        }
        for mapping in target.get("BlockDeviceMappings", []):
            source_volume_id = source_volumes.get(mapping.get("DeviceName"))
            target_volume_id = mapping.get("Ebs", {}).get("VolumeId")
            if not source_volume_id or not target_volume_id:
                continue
            self.sync_resource_tags(
                target_volume_id,
                self.describe_resource_tags(source_volume_id),
                f"volume tags from source volume {source_volume_id}",
            )

        source_enis = {
            eni["Attachment"]["DeviceIndex"]: eni["NetworkInterfaceId"]
            for eni in source.get("NetworkInterfaces", [])
            if eni.get("Attachment", {}).get("DeviceIndex") is not None
        }
        for eni in target.get("NetworkInterfaces", []):
            source_eni_id = source_enis.get(
                eni.get("Attachment", {}).get("DeviceIndex")
            )
            target_eni_id = eni.get("NetworkInterfaceId")
            if not source_eni_id or not target_eni_id:
                continue
            self.sync_resource_tags(
                target_eni_id,
                self.describe_resource_tags(source_eni_id),
                f"network interface tags from source ENI {source_eni_id}",
            )

        logging.info(
            "Completed tag synchronization for AMI %s and replacement instance %s",
            ami_id,
            target_id,
        )

    def describe_new_instance(self, instance_id: str) -> None:
        if instance_id == "i-dryrun":
            return
        instance = get_instance(self.ec2, instance_id)
        logging.info("New instance configuration")
        logging.info("  Instance ID: %s", instance["InstanceId"])
        logging.info("  AZ: %s", instance["Placement"]["AvailabilityZone"])
        logging.info("  Private IP: %s", instance.get("PrivateIpAddress", "N/A"))
        logging.info("  Public IP: %s", instance.get("PublicIpAddress", "N/A"))
        logging.info("  State: %s", instance["State"]["Name"])

    @staticmethod
    def is_dry_run_success(exc: ClientError) -> bool:
        return exc.response.get("Error", {}).get("Code") == "DryRunOperation"


def validate_instance(args: argparse.Namespace) -> int:
    log_file = configure_logging("validation")
    try:
        ec2 = aws_session(args.profile, args.region).client("ec2")
        instance = get_instance(ec2, args.instance_id)
        state = instance["State"]["Name"]
        logging.info("Instance state: %s", state)
        if state != "running":
            logging.error("Instance is not running")
            return 1

        statuses = ec2.describe_instance_status(InstanceIds=[args.instance_id]).get(
            "InstanceStatuses", []
        )
        if statuses:
            status = statuses[0]
            logging.info("System status: %s", status["SystemStatus"]["Status"])
            logging.info("Instance status: %s", status["InstanceStatus"]["Status"])
        else:
            logging.warning("Instance status checks are not available yet")

        logging.info("AZ: %s", instance["Placement"]["AvailabilityZone"])
        logging.info("Private IP: %s", instance.get("PrivateIpAddress", "N/A"))
        logging.info("Public IP: %s", instance.get("PublicIpAddress", "N/A"))
        logging.info("Subnet: %s", instance.get("SubnetId", "N/A"))
        logging.info(
            "Security groups: %s",
            ", ".join(sg["GroupId"] for sg in instance.get("SecurityGroups", []))
            or "N/A",
        )
        logging.info("Validation log: %s", log_file)
        return 0
    except Exception as exc:
        logging.error("Validation failed: %s", exc, exc_info=True)
        return 1


def update_dns(args: argparse.Namespace) -> int:
    log_file = configure_logging("dns_update")
    try:
        session = aws_session(args.profile, args.region)
        ec2 = session.client("ec2")
        route53 = session.client("route53")
        instance = get_instance(ec2, args.instance_id)
        ip_field = "PublicIpAddress" if args.ip_type == "public" else "PrivateIpAddress"
        ip_address = instance.get(ip_field)
        if not ip_address:
            raise ValueError(f"Instance does not have a {args.ip_type} IP address")

        zone_name = clean_name(args.hosted_zone)
        record_name = clean_name(args.record_name)
        zones = route53.list_hosted_zones_by_name(DNSName=zone_name).get(
            "HostedZones", []
        )
        zone = next((z for z in zones if clean_name(z["Name"]) == zone_name), None)
        if not zone:
            raise ValueError(f"Hosted zone {zone_name} was not found")

        record_sets = route53.list_resource_record_sets(
            HostedZoneId=zone["Id"], StartRecordName=record_name, StartRecordType="A"
        )["ResourceRecordSets"]
        existing = next(
            (
                r
                for r in record_sets
                if clean_name(r["Name"]) == record_name and r["Type"] == "A"
            ),
            None,
        )
        ttl = args.ttl or (existing.get("TTL") if existing else 300)
        change = {
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": record_name,
                        "Type": "A",
                        "TTL": ttl,
                        "ResourceRecords": [{"Value": ip_address}],
                    },
                }
            ]
        }
        logging.info("Updating %s to %s in zone %s", record_name, ip_address, zone_name)
        if args.dry_run:
            logging.info("Dry run change batch: %s", json.dumps(change))
            return 0
        response = route53.change_resource_record_sets(
            HostedZoneId=zone["Id"], ChangeBatch=change
        )
        logging.info("Route53 change submitted: %s", response["ChangeInfo"]["Id"])
        logging.info("DNS update log: %s", log_file)
        return 0
    except Exception as exc:
        logging.error("DNS update failed: %s", exc, exc_info=True)
        return 1


def cleanup(args: argparse.Namespace) -> int:
    log_file = configure_logging("cleanup")
    try:
        ec2 = aws_session(args.profile, args.region).client("ec2")
        logging.info("Cleanup inventory for source instance %s", args.instance_id)
        volumes = ec2.describe_volumes(
            Filters=[{"Name": "attachment.instance-id", "Values": [args.instance_id]}]
        )["Volumes"]
        for volume in volumes:
            logging.info(
                "Volume %s: %s GiB, %s, %s",
                volume["VolumeId"],
                volume["Size"],
                volume["VolumeType"],
                volume["State"],
            )

        images = ec2.describe_images(
            Owners=["self"],
            Filters=[
                {
                    "Name": "description",
                    "Values": [f"*Migration AMI for {args.instance_id}*"],
                }
            ],
        )["Images"]
        for image in images:
            logging.info(
                "Migration AMI %s: %s, %s",
                image["ImageId"],
                image["Name"],
                image["State"],
            )

        if args.ami_id:
            image = ec2.describe_images(ImageIds=[args.ami_id])["Images"][0]
            snapshot_ids = [
                mapping["Ebs"]["SnapshotId"]
                for mapping in image.get("BlockDeviceMappings", [])
                if mapping.get("Ebs", {}).get("SnapshotId")
            ]
            logging.info("Deregistering AMI %s", args.ami_id)
            if not args.dry_run:
                ec2.deregister_image(ImageId=args.ami_id)
            if args.delete_snapshots:
                for snapshot_id in snapshot_ids:
                    logging.info("Deleting snapshot %s", snapshot_id)
                    if not args.dry_run:
                        ec2.delete_snapshot(SnapshotId=snapshot_id)

        if args.stop_instance:
            logging.info("Stopping source instance %s", args.instance_id)
            if not args.dry_run:
                ec2.stop_instances(InstanceIds=[args.instance_id])
                ec2.get_waiter("instance_stopped").wait(InstanceIds=[args.instance_id])

        if args.terminate_instance:
            logging.info("Terminating source instance %s", args.instance_id)
            if not args.dry_run:
                ec2.terminate_instances(InstanceIds=[args.instance_id])
                ec2.get_waiter("instance_terminated").wait(
                    InstanceIds=[args.instance_id]
                )

        logging.info("Cleanup log: %s", log_file)
        return 0
    except Exception as exc:
        logging.error("Cleanup failed: %s", exc, exc_info=True)
        return 1


def migrate(args: argparse.Namespace) -> int:
    log_file = configure_logging("migration")
    try:
        migrator = EC2Migrator(
            instance_id=args.instance_id,
            target_az=args.target_az,
            target_subnet=args.target_subnet,
            profile=args.profile,
            region=args.region,
            dry_run=args.dry_run,
            no_reboot=args.no_reboot,
        )
        result = migrator.run()
        result.log_file = log_file
        logging.info("Migration log: %s", log_file)
        return 0
    except Exception as exc:
        logging.error("Migration failed: %s", exc, exc_info=True)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate EC2 instances between Availability Zones"
    )
    parser.add_argument("--profile", default=None, help="AWS profile name")
    parser.add_argument("--region", default=None, help="AWS region")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_aws_options(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--profile", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )
        command_parser.add_argument(
            "--region", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )

    migrate_parser = subparsers.add_parser(
        "migrate", help="Create an AMI and launch a replacement instance"
    )
    add_aws_options(migrate_parser)
    migrate_parser.add_argument("--instance-id", required=True)
    migrate_parser.add_argument("--target-az", required=True)
    migrate_parser.add_argument("--target-subnet", default=None)
    migrate_parser.add_argument("--dry-run", action="store_true")
    migrate_parser.add_argument(
        "--no-reboot",
        action="store_true",
        help="Create the AMI without rebooting the source instance",
    )
    migrate_parser.set_defaults(func=migrate)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate a migrated instance"
    )
    add_aws_options(validate_parser)
    validate_parser.add_argument("--instance-id", required=True)
    validate_parser.set_defaults(func=validate_instance)

    dns_parser = subparsers.add_parser("update-dns", help="Update a Route53 A record")
    add_aws_options(dns_parser)
    dns_parser.add_argument("--hosted-zone", required=True)
    dns_parser.add_argument("--record-name", required=True)
    dns_parser.add_argument("--instance-id", required=True)
    dns_parser.add_argument(
        "--ip-type", choices=("private", "public"), default="private"
    )
    dns_parser.add_argument("--ttl", type=int, default=None)
    dns_parser.add_argument("--dry-run", action="store_true")
    dns_parser.set_defaults(func=update_dns)

    cleanup_parser = subparsers.add_parser(
        "cleanup", help="Inventory and optionally remove migration resources"
    )
    add_aws_options(cleanup_parser)
    cleanup_parser.add_argument("--instance-id", required=True)
    cleanup_parser.add_argument("--ami-id", default=None)
    cleanup_parser.add_argument("--delete-snapshots", action="store_true")
    cleanup_parser.add_argument("--stop-instance", action="store_true")
    cleanup_parser.add_argument("--terminate-instance", action="store_true")
    cleanup_parser.add_argument("--dry-run", action="store_true")
    cleanup_parser.set_defaults(func=cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "stop_instance", False) and getattr(
        args, "terminate_instance", False
    ):
        parser.error("--stop-instance and --terminate-instance are mutually exclusive")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
