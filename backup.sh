#!/usr/bin/env bash
cd /home/kyle/borg-backup
source ./.venv/bin/activate

python3 backup-borg-s3.py

deactivate
