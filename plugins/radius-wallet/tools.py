import json
import os
import subprocess
from pathlib import Path

DEFAULT_CONTAINER_SCRIPTS = Path('/app/scripts/radius')
LOCAL_SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts' / 'radius'
RADIUS_SCRIPTS_DIR = DEFAULT_CONTAINER_SCRIPTS if DEFAULT_CONTAINER_SCRIPTS.exists() else LOCAL_SCRIPTS


def _run_node(script_name: str, args: list[str]) -> str:
    cmd = ['node', str(RADIUS_SCRIPTS_DIR / script_name), *args]
    env = os.environ.copy()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        return json.dumps({
            'ok': False,
            'error': proc.stderr.strip() or proc.stdout.strip() or f"Command failed: {' '.join(cmd)}",
        })

    raw = proc.stdout.strip()
    if not raw:
        return json.dumps({'ok': True})

    candidate = raw
    if '\n{' in raw:
        candidate = '{' + raw.rsplit('\n{', 1)[1]

    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            data.setdefault('ok', True)
            return json.dumps(data)
        return json.dumps({'ok': True, 'data': data})
    except Exception:
        return json.dumps({'ok': True, 'output': raw})


def list_wallets(args: dict, **kwargs) -> str:
    return _run_node('cmd-wallets.mjs', [])


def show_default_wallet(args: dict, **kwargs) -> str:
    result = _run_node('cmd-wallets.mjs', [])
    try:
        parsed = json.loads(result)
        return json.dumps({'ok': True, 'defaultWallet': parsed.get('defaultWallet'), 'wallets': parsed.get('wallets', [])})
    except Exception:
        return json.dumps({'ok': False, 'error': 'Failed to parse default wallet output', 'raw': result})


def switch_default_wallet(args: dict, **kwargs) -> str:
    wallet = str(args.get('wallet', '')).strip().lower()
    if wallet not in {'local', 'para'}:
        return json.dumps({'ok': False, 'error': 'wallet must be local or para'})
    return _run_node('cmd-wallets.mjs', [f'--set-default={wallet}'])


def fund_wallet(args: dict, **kwargs) -> str:
    wallet = str(args.get('wallet', 'all')).strip().lower() or 'all'
    if wallet == 'both':
        wallet = 'all'
    return _run_node('cmd-fund.mjs', [f'--wallet={wallet}'])


def check_balance(args: dict, **kwargs) -> str:
    cmd_args = []
    wallet = args.get('wallet')
    address = args.get('address')
    if wallet:
        cmd_args.append(f'--wallet={wallet}')
    if address:
        cmd_args.append(str(address))
    return _run_node('cmd-balance.mjs', cmd_args)


def send_sbc(args: dict, **kwargs) -> str:
    to = args.get('to')
    amount = args.get('amount')
    if not to or amount is None:
        return json.dumps({'ok': False, 'error': 'to and amount are required'})

    cmd_args = []
    wallet = args.get('wallet')
    asset = str(args.get('asset', 'sbc')).lower()
    if wallet:
        cmd_args.append(f'--wallet={wallet}')
    cmd_args.append(f'--asset={asset}')
    cmd_args.extend([str(to), str(amount)])
    return _run_node('cmd-send.mjs', cmd_args)
