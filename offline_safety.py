"""Install a process-wide offline boundary, with no file writes on import.

This is a research guardrail, not a sandbox for untrusted Python programs.
Wallet SDKs, key stores, HTTP transports and service launchers are not shipped.
"""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'research_outputs'
TEMP = Path(tempfile.gettempdir()).resolve()
_installed = False

def install():
    global _installed
    if _installed:
        return
    sys.dont_write_bytecode = True
    def allowed_write(value):
        p = Path(os.fsdecode(value)).resolve()
        return p.is_relative_to(OUTPUT) or p.is_relative_to(TEMP)
    def audit(event, args):
        if event.startswith('socket.') and event not in ('socket.__new__',):
            raise RuntimeError('OFFLINE_NETWORK_DENIED')
        if event in ('subprocess.Popen', 'os.system', 'os.startfile', 'os.posix_spawn'):
            raise RuntimeError('OFFLINE_PROCESS_LAUNCH_DENIED')
        if event == 'open' and not isinstance(args[0], int):
            p = Path(os.fsdecode(args[0]))
            if p.name.lower() == '.env' or p.suffix.lower() in ('.pem', '.key'):
                raise RuntimeError('CREDENTIAL_FILE_READ_DENIED')
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
                if not allowed_write(p):
                    raise RuntimeError('OFFLINE_WRITE_OUTSIDE_RESEARCH_OUTPUT_DENIED')
        if event == 'sqlite3.connect':
            value = os.fsdecode(args[0])
            if value not in ('', ':memory:') and not (value.startswith('file:') and 'mode=ro' in value.split('?')[-1]):
                if not allowed_write(value):
                    raise RuntimeError('PRIVATE_DATABASE_WRITE_DENIED')
        if event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.rename', 'os.replace'):
            values = args[:2] if event in ('os.rename', 'os.replace') else args[:1]
            if any(not isinstance(v, int) and not allowed_write(v) for v in values):
                raise RuntimeError('OFFLINE_MUTATION_OUTSIDE_RESEARCH_OUTPUT_DENIED')
    sys.addaudithook(audit)
    _installed = True
