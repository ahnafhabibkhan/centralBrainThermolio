#!/usr/bin/env bash
set -euo pipefail
umask 077
role=$(cat /opt/central-brain/library-role-arn)
aws sts assume-role --role-arn "$role" --role-session-name library-storage --duration-seconds 3600 --output json |
python3 -c '
import json,sys,os
from pathlib import Path
c=json.load(sys.stdin)["Credentials"]
p=Path("/opt/central-brain/library-credentials")
p.mkdir(mode=0o755,exist_ok=True)
os.chmod(p,0o755)
f=p/"credentials.new"
f.write_text("[default]\naws_access_key_id="+c["AccessKeyId"]+"\naws_secret_access_key="+c["SecretAccessKey"]+"\naws_session_token="+c["SessionToken"]+"\n")
os.chown(f,10001,10001);os.chmod(f,0o400)
f.replace(p/"credentials")
'
