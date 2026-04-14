LIST_WALLETS = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

SHOW_DEFAULT_WALLET = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

SWITCH_DEFAULT_WALLET = {
    "type": "object",
    "properties": {
        "wallet": {"type": "string", "enum": ["local", "para"]},
    },
    "required": ["wallet"],
    "additionalProperties": False,
}

FUND_WALLET = {
    "type": "object",
    "properties": {
        "wallet": {"type": "string", "description": "local, para, or all", "default": "all"},
    },
    "additionalProperties": False,
}

CHECK_BALANCE = {
    "type": "object",
    "properties": {
        "wallet": {"type": "string", "enum": ["local", "para"]},
        "address": {"type": "string", "description": "Optional explicit address"},
    },
    "additionalProperties": False,
}

SEND_SBC = {
    "type": "object",
    "properties": {
        "to": {"type": "string"},
        "amount": {"type": "string", "description": "Token amount as decimal string"},
        "wallet": {"type": "string", "enum": ["local", "para"]},
        "asset": {"type": "string", "enum": ["sbc", "rusd"], "default": "sbc"},
    },
    "required": ["to", "amount"],
    "additionalProperties": False,
}
