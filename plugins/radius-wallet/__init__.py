from . import schemas, tools


def register(ctx):
    ctx.register_tool(
        name="list_wallets",
        toolset="radius_wallet",
        schema=schemas.LIST_WALLETS,
        handler=tools.list_wallets,
    )
    ctx.register_tool(
        name="show_default_wallet",
        toolset="radius_wallet",
        schema=schemas.SHOW_DEFAULT_WALLET,
        handler=tools.show_default_wallet,
    )
    ctx.register_tool(
        name="switch_default_wallet",
        toolset="radius_wallet",
        schema=schemas.SWITCH_DEFAULT_WALLET,
        handler=tools.switch_default_wallet,
    )
    ctx.register_tool(
        name="fund_wallet",
        toolset="radius_wallet",
        schema=schemas.FUND_WALLET,
        handler=tools.fund_wallet,
    )
    ctx.register_tool(
        name="check_wallet_balance",
        toolset="radius_wallet",
        schema=schemas.CHECK_BALANCE,
        handler=tools.check_balance,
    )
    ctx.register_tool(
        name="send_wallet_transfer",
        toolset="radius_wallet",
        schema=schemas.SEND_SBC,
        handler=tools.send_sbc,
    )
