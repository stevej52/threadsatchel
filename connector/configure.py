"""Create a Windows current-user encrypted connector configuration."""
import argparse
from getpass import getpass
import os
from pathlib import Path
import re

from .connector import encode, protect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path, help='Initialized archive directory')
    parser.add_argument('--endpoint', required=True, help='HTTPS URL ending in /api/agent')
    parser.add_argument('--output', required=True, type=Path, help='New encrypted file to create')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This command uses Windows DPAPI. Use environment settings on other platforms.')
    archive = args.archive.expanduser().resolve()
    if not (archive / 'memory.sqlite3').is_file():
        parser.error('The archive must contain an initialized memory.sqlite3.')
    if not re.fullmatch(r'https://[a-z0-9.-]+/api/agent', args.endpoint):
        parser.error('Specify the HTTPS gateway URL ending in /api/agent.')
    key = getpass('Connector key: ')
    access_token = getpass('Connector access token: ')
    if min(len(key), len(access_token)) < 32 or key == access_token:
        parser.error('Supply two distinct secrets of at least 32 characters each.')
    ciphertext = protect(encode({'archive':str(archive), 'endpoint':args.endpoint,
                                 'key':key, 'access_token':access_token}))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('xb') as file:
        file.write(ciphertext)
    print('Encrypted configuration created:', args.output)


if __name__ == '__main__':
    main()
