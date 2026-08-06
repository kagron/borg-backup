#!/usr/bin/env python3
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime

from dotenv import load_dotenv

# Get environment variables from .env
load_dotenv()

LOG_PATH = os.environ.get("LOG_PATH")
LOG_LEVEL = os.environ.get("LOG_LEVEL")
NUM_LOG_LEVEL = getattr(logging, LOG_LEVEL.upper(), None)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
    filename=LOG_PATH,
    level=NUM_LOG_LEVEL,
)
logger = logging.getLogger(__name__)

HOME_BACKUP_PREFIX = "home-backup"
ROUTER_BACKUP_PREFIX = "router-backup"
PIHOLE_BACKUP_PREFIX = "pihole-backup"
ETC_BACKUP_PREFIX = "etc-backup"
NEXTCLOUD_BACKUP_PREFIX = "nextcloud-backup"
SAVES_BACKUP_PREFIX = "saves-backup"
ALL_PREFIXES = (
    HOME_BACKUP_PREFIX,
    ROUTER_BACKUP_PREFIX,
    PIHOLE_BACKUP_PREFIX,
    NEXTCLOUD_BACKUP_PREFIX,
    SAVES_BACKUP_PREFIX,
    ETC_BACKUP_PREFIX,
)
CURRENT_TIME = datetime.now().strftime("%Y-%m-%dT%H.%M")
PIHOLE_BACKUP_DIR = "pi-hole-backup"
ROUTER_BACKUP_DIR = "openwrt-backup"
ROUTER_TAR_NAME = "openwrt.tar.gz"
DEBUG = False

ENV_VARS = (
    "ROUTER_HOST",
    "PIHOLE_HOST",
    "SSH_PRIVATE_KEY_PATH",
    "BORG_REPO",
    "BORG_EXTDRIVE_REPO",
    "BORG_S3_BACKUP_BUCKET",
    "BORG_S3_BACKUP_AWS_PROFILE",
    "NEXTCLOUD_HOME",
    "SAVES_DIR",
    "GOTIFY_URL",
    "GOTIFY_API_KEY",
)


def backup():
    """Backup all the goodies"""
    logger.info(f"Starting backup {CURRENT_TIME}")

    # Prepare backup directories
    create_router_archive = not get_router_backup()
    create_pihole_archive = not get_pihole_backup()

    stop_docker()

    borg_repo = os.environ.get("BORG_REPO")
    borg_ext_repo = os.environ.get("BORG_EXTDRIVE_REPO")

    # Handled in main method, but this tells pyright to shut up
    assert borg_repo is not None
    assert borg_ext_repo is not None

    try:
        backup_to_repo(borg_repo, create_router_archive, create_pihole_archive)
    except subprocess.CalledProcessError as error:
        logger.error("Did not backup to AWS nor ext drive")
        logger.error(f"Error running command '{error.cmd}'")
        send_notification("Backup Failed!", error.output)
        cleanup()
        start_docker()
        sys.exit(1)

    borg_info = ""

    try:
        prune_repo(borg_repo)
    except subprocess.CalledProcessError as error:
        logger.error(f"Failed to prune repo '{borg_repo}'")
        send_notification(
            f"Failed to prune repo '{borg_repo}'",
            error.output,
        )

    try:
        compact_repo(borg_repo)
    except subprocess.CalledProcessError as error:
        logger.error(f"Failed to compact repo '{borg_repo}'!")
        logger.error(error.output)
        send_notification(f"Failed to compact repo '{borg_repo}'", error.output)

    # Let OS clean up maybe?  Rclone is reporting files are being
    # modified when rclone starts
    time.sleep(25)

    try:
        clone(borg_repo, borg_ext_repo)
    except subprocess.CalledProcessError as error:
        logger.error("Failed to clone!")
        logger.error(error.output)
        send_notification("Failed to clone repos!", error.output)

    try:
        backup_to_aws(borg_repo)
    except subprocess.CalledProcessError as error:
        logger.error("Failed to backup to AWS!")
        logger.error(error.output)
        send_notification("Backup to AWS Failed!", error.output)
        cleanup()
        start_docker()
        sys.exit(1)

    borg_info = get_repo_info(borg_repo)

    cleanup()

    start_docker()

    aws_bucket_size = get_aws_bucket_size()

    logger.info(f"Borg repo {borg_repo} stats: \n{borg_info}")
    logger.info(f"AWS bucket size: {aws_bucket_size}")

    send_notification(
        "Backup Successful",
        f"Borg NAS Stats: \n{borg_info}\nAWS " + f"bucket size: {aws_bucket_size}",
    )


def borg_create(
    borg_repo: str, backup_name: str, backup_dir: str, excludes_file: str, dry_run=False
) -> int:
    """Creates a borg archive"""
    logger.info(
        f"Backing up {backup_dir} with borg to " + f"{borg_repo}::{backup_name}"
    )
    cmd = [
        "borg create "
        + ("--dry-run " if dry_run else "")
        + f"{borg_repo}::{backup_name} "
        + f"{backup_dir} "
        + ("--stats " if not dry_run else "-v ")
        + f"--exclude-from {excludes_file} "
        + "--compression zlib,6"
    ]
    result = subprocess.run(cmd, check=True, shell=True, capture_output=True, text=True)

    log_result(result)

    return result.returncode


def ssh(host: str, command: str):
    """Runs a ssh command"""
    logger.info(f"Initiating ssh command: {host} {command}")
    private_key_path = os.environ.get("SSH_PRIVATE_KEY_PATH")

    return subprocess.run(
        [f"ssh -i {private_key_path} {host} {command}"], check=True, shell=True
    )


def scp(host: str, remote_path: str, local_path: str):
    """Runs a scp command"""
    private_key_path = os.environ.get("SSH_PRIVATE_KEY_PATH")
    logger.info(f"Initiating scp command: {host}:{remote_path} {local_path}")

    # -O to use old legacy SCP protocol instead of SFTP
    return subprocess.run(
        [f"scp -O -i {private_key_path} {host}:{remote_path} {local_path}"],
        check=True,
        shell=True,
    )


def get_router_backup() -> int:
    """Retrieves /etc config files from router.  Returns 0 when successful"""
    logger.info("Retrieving Openwrt.lan backup")

    router_host = os.environ.get("ROUTER_HOST")
    user_and_host = f"root@{router_host}"
    result = None

    try:
        log_result(ssh(user_and_host, f"tar -cvzf {ROUTER_TAR_NAME} /etc"))
        log_result(scp(user_and_host, ROUTER_TAR_NAME, "."))
        log_result(ssh(user_and_host, f"rm -rf {ROUTER_TAR_NAME}"))
    except subprocess.CalledProcessError as error:
        send_notification(
            title=f"Error retrieving {router_host} backup", message=error.output
        )
        return 1

    os.mkdir(ROUTER_BACKUP_DIR)

    result = subprocess.run(
        ["tar", "xzvf", ROUTER_TAR_NAME, "-C", ROUTER_BACKUP_DIR], check=False
    )

    log_result(result)

    return result.returncode


def get_pihole_backup() -> int:
    """Retrieves /etc config files from pihole.  Returns 0 when successful"""
    logger.info("Retrieving Pi-Hole backup")

    pihole_host = os.environ.get("PIHOLE_HOST")
    user_and_host = f"pi@{pihole_host}"
    result = None

    try:
        log_result(ssh(user_and_host, "sudo pihole-FTL --teleporter"))
        log_result(scp(user_and_host, "pi-hole*", "."))
        log_result(ssh(user_and_host, "rm -rf pi-hole*"))
    except subprocess.CalledProcessError as error:
        send_notification("Error retrieving Pi-Hole backup", error.output)
        return 1

    os.mkdir(PIHOLE_BACKUP_DIR)

    result = subprocess.run(
        [f"unzip pi-hole_raspberrypi_teleporter* -d {PIHOLE_BACKUP_DIR}"],
        shell=True,
        check=False,
    )

    log_result(result)

    return result.returncode


def backup_to_repo(
    borg_repo: str, create_router_archive: bool, create_pihole_archive: bool
):
    """
    Performs the backups to repo.  Will conditionally back up router and
    pi-hole based on flags
    """
    logger.info(f"Backing up to repo {borg_repo}")
    excludes = "excludes.txt"
    nextcloud_home = os.environ.get("NEXTCLOUD_HOME")
    saves_dir = os.environ.get("SAVES_DIR")

    # Handled in main method, but this tells pyright to shut up
    assert nextcloud_home is not None
    assert saves_dir is not None

    # Home
    borg_create(
        borg_repo=borg_repo,
        backup_name=f"{HOME_BACKUP_PREFIX}-{CURRENT_TIME}",
        backup_dir="/home",
        excludes_file=excludes,
        dry_run=DEBUG,
    )

    # Router
    if create_router_archive:
        borg_create(
            borg_repo=borg_repo,
            backup_name=f"{ROUTER_BACKUP_PREFIX}-{CURRENT_TIME}",
            backup_dir=ROUTER_BACKUP_DIR,
            excludes_file=excludes,
            dry_run=DEBUG,
        )

    # Pihole
    if create_pihole_archive:
        borg_create(
            borg_repo=borg_repo,
            backup_name=f"{PIHOLE_BACKUP_PREFIX}-{CURRENT_TIME}",
            backup_dir=PIHOLE_BACKUP_DIR,
            excludes_file=excludes,
            dry_run=DEBUG,
        )

    # /etc
    borg_create(
        borg_repo=borg_repo,
        backup_name=f"{ETC_BACKUP_PREFIX}-{CURRENT_TIME}",
        backup_dir="/etc",
        excludes_file=excludes,
        dry_run=DEBUG,
    )

    # Nextcloud Home
    borg_create(
        borg_repo=borg_repo,
        backup_name=f"{NEXTCLOUD_BACKUP_PREFIX}-{CURRENT_TIME}",
        backup_dir=nextcloud_home,
        excludes_file=excludes,
        dry_run=DEBUG,
    )

    # Saves
    borg_create(
        borg_repo=borg_repo,
        backup_name=f"{SAVES_BACKUP_PREFIX}-{CURRENT_TIME}",
        backup_dir=saves_dir,
        excludes_file=excludes,
        dry_run=DEBUG,
    )


def stop_docker():
    """Stops all running docker containers"""
    logger.info("Stopping docker containers")

    result = subprocess.run(
        ["docker stop $(docker ps -a -q)"],
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )
    log_result(result)


def start_docker():
    """Starts all docker containers"""
    logger.info("Starting docker containers")

    result = subprocess.run(
        ["docker start $(docker ps -a -q)"],
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )
    log_result(result)


def send_notification(title: str, message: str, priority=0):
    """Sends notification to gotify"""
    logger.info("Sending notification to gotify")

    gotify_url = os.environ.get("GOTIFY_URL")
    gotify_api_key = os.environ.get("GOTIFY_API_KEY")

    cmd = [
        f"curl -s {gotify_url} "
        + f'-H "X-Gotify-Key: {gotify_api_key}" '
        + f'-F "title={title}" '
        + f'-F "message={message}" '
        + f'-F "priority={priority}"'
    ]
    # Hopefully this never fails :)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, check=False
    )

    log_result(result)


def prune_repo(borg_repo: str):
    """Prune old archives from borg repo"""
    logger.info(f"Pruning old backups from repo {borg_repo}")

    for prefix in ALL_PREFIXES:
        result = subprocess.run(
            [
                f"borg prune -v -P {prefix} --list --keep-daily=3 "
                + f"--keep-weekly=2 --keep-monthly=3 {borg_repo}"
            ],
            shell=True,
            capture_output=True,
            text=True,
            check=True,
        )

        log_result(result)


def compact_repo(borg_repo: str):
    """Compact repo to free up deleted data"""
    logger.info(f"Compacting data from repo {borg_repo}")

    result = subprocess.run(
        [f"borg compact -v {borg_repo}"],
        shell=True,
        capture_output=True,
        text=True,
        check=True,
    )

    log_result(result)


def get_repo_info(borg_repo: str, backup_name="", json=False) -> str:
    """Runs a borg info command"""
    logger.info(f"Running borg info {borg_repo}")
    try:
        result = subprocess.run(
            [
                "borg info "
                + ("--json " if json else "")
                + borg_repo
                + (f"::{backup_name}" if backup_name != "" else "")
            ],
            capture_output=True,
            text=True,
            shell=True,
            check=True,
        )

        log_result(result)
    except subprocess.CalledProcessError as error:
        logger.error("Failed to retrieve borg info!")
        logger.error(f"Ran command '{error.cmd}'")
        logger.error(error.output)
        send_notification("Failed to retrieve borg info!", error.output)
        return ""

    return result.stdout


def check() -> int:
    """
    Runs a borg check command monthly on first Monday for BORG_REPO
    and first Tuesday for BORG_EXTDRIVE_REPO
    """
    today = date.today()
    result = None
    repo = None

    if today.day >= 1 and today.day <= 7 and today.weekday() == 0:
        repo = os.environ.get("BORG_REPO")
    elif today.day >= 1 and today.day <= 7 and today.weekday() == 1:
        repo = os.environ.get("BORG_EXTDRIVE_REPO")

    if repo is not None:
        logger.info("Running check")

        cmd = [f"borg check --verify-data {repo}"]
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, check=False
        )
        log_result(result)

        if result.returncode:
            send_notification(
                "Check Failed",
                f"Borg check --verify-data failed for repo '{repo}'",
            )
        else:
            send_notification(
                "Check Successful",
                f"Borg check --verify-data was successful for repo '{repo}'",
            )

    return result.returncode if result is not None else 0


def clone(source: str, target: str):
    """
    Runs rclone sync to keep backups in sync WITH BORG LOCK
    """
    logger.info(f"Starting rclone sync {source} {target}")

    result = subprocess.run(
        [f"borg with-lock {source} " + f"rclone sync {source} {target}"],
        shell=True,
        text=True,
        capture_output=True,
        check=True,
    )

    log_result(result)


def get_backup_size(borg_repo: str, backup_name="") -> int:
    """Gets backup size.  Total backup size if no backup_name specified"""
    logger.info(
        f"Getting borg backup size for: {borg_repo}"
        + (f"::{backup_name}" if backup_name != "" else "")
    )

    result = subprocess.run(
        [
            f"borg info --json {borg_repo}"
            + (f"::{backup_name} " if backup_name != "" else " ")
            + "| "
            + "jq .cache.stats.unique_csize | "
            + "awk '{ printf \"%d\", $1/1024/1024/1024; }'"
        ],
        capture_output=True,
        text=True,
        shell=True,
        check=False,
    )

    log_result(result)

    return int(result.stdout) if not result.returncode else 0


def backup_to_aws(borg_repo: str):
    """Syncs borg repo to AWS WITH BORG LOCK."""
    s3_bucket = os.environ.get("BORG_S3_BACKUP_BUCKET")
    s3_profile = os.environ.get("BORG_S3_BACKUP_AWS_PROFILE")
    backup_threshold = int(os.environ.get("BACKUP_THRESHOLD", "0"))

    if backup_threshold > 0:
        backup_size = int(get_backup_size(borg_repo))
        if backup_size > backup_threshold:
            msg = (
                f"Backup size {backup_size} GB is larger than threshold "
                + f"{backup_threshold} GB"
            )

            logger.error(msg)
            send_notification(title="Backup Threshold", message=msg)
            return 1

    logger.info(f"Syncing to s3 bucket {s3_bucket}")
    subprocess.run(
        [
            f"borg with-lock {borg_repo} "
            + f"aws s3 sync {borg_repo} s3://{s3_bucket} "
            + f"--profile={s3_profile} --delete"
        ],
        shell=True,
        check=True,
    )


def get_aws_bucket_size() -> str:
    """Retrieves current AWS S3 Bucket Size.  Returns AWS bucket size when successful"""
    s3_bucket = os.environ.get("BORG_S3_BACKUP_BUCKET")
    s3_profile = os.environ.get("BORG_S3_BACKUP_AWS_PROFILE")

    logger.info(f"Getting aws bucket size {s3_bucket}")
    result = subprocess.run(
        [
            f"aws s3 ls --profile={s3_profile} --summarize --recursive "
            + f"s3://{s3_bucket} | "
            + "tail -1 | "
            + "awk '{ printf \"%.3f GB\", $3/1024/1024/1024; }'"
        ],
        capture_output=True,
        text=True,
        shell=True,
        check=False,
    )

    log_result(result)

    return result.stdout if not result.returncode else ""


def cleanup():
    """Cleans up directory"""
    logger.info("Cleanup")

    logger.debug(subprocess.run(["rm -rf openwrt*"], shell=True, check=False))
    logger.debug(subprocess.run(["rm -rf pi-hole*"], shell=True, check=False))


def log_result(result: subprocess.CompletedProcess):
    """Logs result with either ERROR or DEBUG based on return code"""
    stdout = result.stdout
    stderr = result.stderr
    stdoutlines = []
    stderrlines = []

    if stdout is not None and len(stdout) > 0:
        stdoutlines = stdout.split("\n")

    if stderr is not None and len(stderr) > 0:
        stderrlines = stderr.split("\n")

    if result.returncode == 0:
        logger.debug(f"Ran command: '{result.args}' successfully")
        if len(stdoutlines) > 0:
            logger.debug("Stdout:")
        for line in stdoutlines:
            logger.debug(line)

        if len(stderrlines) > 0:
            logger.debug("Stderr:")
        for line in stderrlines:
            logger.debug(line)

    else:
        logger.error(f"Failed command: '{result.args}'!")
        if len(stdoutlines) > 0:
            logger.error("Stdout:")
        for line in stdoutlines:
            logger.error(line)

        if len(stderrlines) > 0:
            logger.error("Stderr:")
        for line in stderrlines:
            logger.error(line)


if __name__ == "__main__":
    # Backup
    # Check

    # Verify all required variables are set
    for env_var in ENV_VARS:
        if not os.environ.get(env_var):
            logger.error(f"Please provide {env_var} in .env file")
            sys.exit(1)

    backup()
    check()
