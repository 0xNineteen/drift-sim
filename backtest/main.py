import sys
sys.path.insert(0, '../')
sys.path.insert(0, '../driftpy/src/')

import pandas as pd 
import numpy as np 

from driftpy.math.amm import *
from driftpy.math.trade import *
from driftpy.math.positions import *
from driftpy.math.market import *
from driftpy.math.user import *

from driftpy.types import *
from driftpy.types import PerpMarket
from driftpy.constants.numeric_constants import *

from driftpy.setup.helpers import _create_usdc_mint, mock_oracle, _airdrop_user, set_price_feed, set_price_feed_detailed, adjust_oracle_pretrade, _mint_usdc_tx, _create_user_usdc_ata_tx
from driftpy.clearing_house import ClearingHouse
from driftpy.admin import Admin
from driftpy.types import OracleSource

from sim.events import * 
from driftpy.clearing_house import ClearingHouse as SDKClearingHouse
from driftpy.accounts import get_perp_market_account, get_spot_market_account, get_user_account, get_state_account
from driftpy.math.amm import calculate_mark_price_amm

from anchorpy import Provider, Program, create_workspace, close_workspace
from sim.driftsim.clearing_house.state.market import SimulationAMM, SimulationMarket
from tqdm import tqdm
from driftpy.setup.helpers import _create_user_usdc_ata_tx
from driftpy.clearing_house_user import ClearingHouseUser
from solana.keypair import Keypair

from termcolor import colored
from subprocess import Popen
import time 
from solana.transaction import TransactionInstruction
from client.instructions.place_order import layout

from parsing import *
from helpers import *
from setup import *
from liquidator import Liquidator
from solana.rpc.core import RPCException
from anchorpy.coder.common import _sighash

# set inside run_trail()
LOGGER: Logger = None
    
async def send_ix(
    ch: ClearingHouse, 
    ix: TransactionInstruction, 
    event_name: str, 
    ix_args: dict, 
    silent_fail=False, 
    silent_success=False,
):
    global LOGGER

    failed = 1 # 1 = fail, 0 = success
    provider = ch.program.provider
    slot = (await provider.connection.get_slot())['result']
    err = None
    try:
        if event_name == SettleLPEvent._event_name:
            await ch.send_ixs(ix, signers=[])
        else:
            await ch.send_ixs(ix)
        failed = 0
    except RPCException as e:
        err = e.args
    
    LOGGER.log(slot, event_name, ix_args, err)

    if not failed and not silent_success: 
        print(colored(f'> {event_name} success', "green"))
    elif failed and not silent_fail:
        print(colored(f'> {event_name} failed', "red"))
        pprint.pprint(err)
            
    return failed

async def run_trial(protocol_path, events, markets, trial_outpath, oracle_guard_rails=None, spread=None):
    print('trial_outpath:', trial_outpath)
    print('protocol path:', protocol_path)

    workspace = create_workspace(protocol_path)
    program: Program = workspace["clearing_house"]
    oracle_program: Program = workspace["pyth"]
    provider: Provider = program.provider

    admin_clearing_house, usdc_mint = await setup_bank(program)

    # state modification
    await admin_clearing_house.update_perp_auction_duration(0)
    await admin_clearing_house.update_lp_cooldown_time(0)    

    if oracle_guard_rails is not None:
        await admin_clearing_house.update_oracle_guard_rails(oracle_guard_rails)

    # setup the markets
    init_leverage = 11
    n_markets = len(markets) 
    last_oracle_prices = []

    for i, market in enumerate(markets):
        oracle_price = await setup_market(
            admin_clearing_house, 
            oracle_program, 
            market, 
            i,
            init_leverage, 
            spread
        )
        last_oracle_prices.append(oracle_price)

    # save initial state
    state_path = f'{trial_outpath}/init_state.json' 
    await save_state_account(program, state_path)

    start = time.time()

    # initialize users + deposits
    user_path = f'{trial_outpath}/users.json'
    users, liquidator_index = await airdrop_sol_users(provider, events, user_path)
    user_chs, _user_chus, _init_total_collateral = await setup_usdc_deposits(events, program, usdc_mint, users, liquidator_index)

    # compute init collateral 
    async def get_token_amount(usdc_ata: Keypair):
        return (await provider.connection.get_token_account_balance(usdc_ata.public_key))['result']['value']['uiAmount']
    async def get_collateral_amount(chu: ClearingHouseUser):
        return (await chu.get_total_collateral()) / QUOTE_PRECISION
    async def compute_collateral_amount():
        promises = []
        ch: ClearingHouse
        for (i, ch) in user_chs.items():
            chu: ClearingHouseUser = _user_chus[i]
            # get it all in $ amounts
            user_collateral = get_collateral_amount(chu)
            token_amount = get_token_amount(ch.usdc_ata)
            promises.append(user_collateral)
            promises.append(token_amount)
        all_user_collateral = sum(await asyncio.gather(*promises))

        return all_user_collateral

    init_total_collateral = await compute_collateral_amount()
    print(f'> initial collateral: {init_total_collateral}')
    assert int(init_total_collateral) == int(_init_total_collateral/QUOTE_PRECISION)

    liquidator = Liquidator(user_chs, n_markets, liquidator_index, send_ix)

    global LOGGER
    LOGGER = Logger(f'{trial_outpath}/ix_logs.csv')

    # process events 
    for i in tqdm(range(len(events))):
        sys.stdout.flush()
        event = events.iloc[i]
        ix_args = None

        ix: TransactionInstruction
        if event.event_name == DepositCollateralEvent._event_name:
            continue

        elif event.event_name == OpenPositionEvent._event_name: 
            event = Event.deserialize_from_row(OpenPositionEvent, event)
            print(f'=> {event.user_index} opening position...')
            assert event.user_index in user_chs, 'user doesnt exist'

            ch: SDKClearingHouse = user_chs[event.user_index]
            ix = await event.run_sdk(ch, init_leverage, oracle_program, adjust_oracle_pre_trade=False)
            if ix is None:
                continue
            ix_args = place_and_take_ix_args(ix[1])

        elif event.event_name == ClosePositionEvent._event_name: 
            # dont close so we have stuff to settle at end of sim
            continue

            # event = Event.deserialize_from_row(ClosePositionEvent, event)
            # print(f'=> {event.user_index} closing position...')
            # assert event.user_index in user_chs, 'user doesnt exist'
            
            # ch: SDKClearingHouse = user_chs[event.user_index]
            # await event.run_sdk(ch, oracle_program, adjust_oracle_pre_trade=True)

        elif event.event_name == addLiquidityEvent._event_name: 
            event = Event.deserialize_from_row(addLiquidityEvent, event)
            print(f'=> {event.user_index} adding liquidity: {event.token_amount}...')
            assert event.user_index in user_chs, 'user doesnt exist'

            ch: SDKClearingHouse = user_chs[event.user_index]
            ix = await event.run_sdk(ch)
            ix_args = add_liquidity_ix_args(ix)
            
        elif event.event_name == removeLiquidityEvent._event_name:
            event = Event.deserialize_from_row(removeLiquidityEvent, event)
            print(f'=> {event.user_index} removing liquidity...')
            assert event.user_index in user_chs, 'user doesnt exist'
            event.lp_token_amount = -1

            ch: SDKClearingHouse = user_chs[event.user_index]
            ix = await event.run_sdk(ch)
            if ix is None: 
                continue
            ix_args = remove_liquidity_ix_args(ix)

        elif event.event_name == SettlePnLEvent._event_name:
            event = Event.deserialize_from_row(SettlePnLEvent, event)
            print(f'=> {event.user_index} settle pnl...')
            ch: SDKClearingHouse = user_chs[event.user_index]
            ix = await event.run_sdk(ch)
            ix_args = settle_pnl_ix_args(ix)

        elif event.event_name == SettleLPEvent._event_name: 
            event = Event.deserialize_from_row(SettleLPEvent, event)
            print(f'=> {event.user_index} settle lp...')
            ch: SDKClearingHouse = user_chs[event.user_index]
            ix = await event.run_sdk(ch)
        
        elif event.event_name == oraclePriceEvent._event_name: 
            event = Event.deserialize_from_row(oraclePriceEvent, event)
            event.slot = (await provider.connection.get_slot())['result']
            print(f'=> adjusting oracle: {event.price}')
            await event.run_sdk(program, oracle_program)

        elif event.event_name == 'liquidate':
            print('=> liquidating...')
            await liquidator.liquidate_loop()
            continue
        
        elif event.event_name == NullEvent._event_name: 
            continue
        else:
            raise NotImplementedError

        need_to_send_ix = ix_args is not None 
        if need_to_send_ix:
            await send_ix(ch, ix, event._event_name, ix_args)        

        # try to liquidate traders after each timestep 
        await liquidator.liquidate_loop()

    print('delisting market...')
    slot = (await provider.connection.get_slot())['result']
    dtime: int = (await provider.connection.get_block_time(slot))['result']

    # + N seconds
    seconds_time = 2
    for i in range(n_markets):
        await admin_clearing_house.update_perp_market_expiry(i, dtime + seconds_time)
    
    # close out all the LPs 
    routines = []
    for i in range(n_markets):
        for (_, ch) in user_chs.items():
            position = await ch.get_user_position(i)
            if position is None: 
                continue
            if position.lp_shares > 0:
                print(f'removing {position.lp_shares} lp shares...')
                routines.append(
                    ch.remove_liquidity(position.lp_shares, position.market_index)
                )
    await asyncio.gather(*routines)

    # settle expired market
    time.sleep(seconds_time)
    for i in range(n_markets):
        await admin_clearing_house.settle_expired_market(i)

    for i in range(n_markets):
        market = await get_perp_market_account(
            program, i
        )
        print(
            'market expiry_price vs twap/price', 
            market.expiry_price, 
            market.amm.historical_oracle_data.last_oracle_price_twap,
            market.amm.historical_oracle_data.last_oracle_price
        )

    # liquidate em + resolve bankrupts
    await liquidator.liquidate_loop()

    # cancel open orders
    routines = []
    for (i, ch) in user_chs.items():
        position = await ch.get_user_position(0)
        if position is not None and position.open_orders > 0:
            routines.append(
                ch.cancel_order()
            )
    await asyncio.gather(*routines)

    # settle expired positions 
    print('settling expired positions')

    for i in range(n_markets):
        number_positions = 0
        for (_, ch) in user_chs.items():
            position = await ch.get_user_position(i)
            if position is None: 
                continue
            number_positions += 1
        print('number of positions:', number_positions)

        market = await get_perp_market_account(program, i)
        print(
            'net long/short',
            market.amm.base_asset_amount_long, 
            market.amm.base_asset_amount_short, 
            market.number_of_users, 
        )

    for i in range(n_markets):
        routines = []
        success = False
        attempt = -1
        user_withdraw_count = 0
        n_users = len(list(user_chs.keys()))
        last_oracle_price = last_oracle_prices[i]

        while not success:
            attempt += 1
            success = True
            user_count = 0
            user_withdraw_count = 0

            print(colored(f' =>>>>>> attempt {attempt}', "blue"))
            for (_, ch) in user_chs.items():
                position = await ch.get_user_position(i)

                # dont let oracle go stale
                slot = (await provider.connection.get_slot())['result']
                market = await get_perp_market_account(program, i)
                await set_price_feed_detailed(oracle_program, market.amm.oracle, last_oracle_price, 0, slot)

                if position is None: 
                    user_count += 1
                else:
                    try:
                        settling_position = await ch.get_user_position(i)
                        scaled_balance = (await ch.get_user()).spot_positions[0].scaled_balance
                        print('user', i, str(ch.authority), ': ', '$',scaled_balance/SPOT_BALANCE_PRECISION)
                        print('settling position:', settling_position)

                        await ch.settle_pnl(ch.authority, i)
                        user_count += 1
                        print(colored(f'     *** settle expired position successful {user_count}/{n_users} ***   ', "green"))
                    except Exception as e:
                        if "0x17e2" in e.args[0]['message']: # pool doesnt have enough 
                            print(colored(f'     *** settle expired position failed... ***   ', "red"))
                            success = False
                        elif "0x17c0" in e.args[0]['message']: # pool doesnt have enough 
                            print(colored(f'     *** settle expired position failed InsufficientCollateralForSettlingPNL... ***   ', "red"))
                            success = False
                        else: 
                            raise Exception(e)

                try:
                    # try to withdraw all you can
                    print(f'=> user {i} withdrawing...')
                    await ch.withdraw(
                        int(1e10 * 1e6), 
                        0, 
                        ch.usdc_ata.public_key,
                        True
                    )
                    print(colored(f'     *** user withdraw successful {user_withdraw_count}/{n_users} ***   ', "green"))
                except Exception as e:
                    print(e)
                    print(colored(f'     *** user withdraw failed {user_withdraw_count}/{n_users} ***   ', "red"))
                    pass
                
                market = await get_perp_market_account(program, i)
                print(
                    'net long/short',
                    market.amm.base_asset_amount_long, 
                    market.amm.base_asset_amount_short, 
                    market.number_of_users, 
                )

    for i in range(n_markets):
        for (_, ch) in user_chs.items():
            position = await ch.get_user_position(i)
            if position is None: 
                continue
            print(position)

    # skip for now
    # await admin_clearing_house.settle_expired_market_pools_to_revenue_pool(0)

    market = await get_perp_market_account(program, i)
    print(market.status)

    # df = pd.DataFrame(df_rows)
    # df.to_csv('tmp.csv', index=False)

    # # close out anyone who hasnt already closed out 
    # print('closing out everyone...')
    # net_baa = 1 
    # market = await get_perp_market_account(program, 0)
    # max_n_tries = 4
    # n_tries = 0
    # while net_baa != 0 and n_tries < max_n_tries:
    #     n_tries += 1
    #     net_baa = 0
    #     for (i, ch) in tqdm(user_chs.items()):
    #         position = await ch.get_user_position(0)
    #         if position is None: 
    #             continue
    #         net_baa += abs(position.base_asset_amount)

    #         if position.lp_shares > 0:
    #             await ch.remove_liquidity(position.lp_shares, position.market_index)
    #             position = await ch.get_user_position(0)

    #         if position.base_asset_amount != 0: 
    #             await ch.close_position(position.market_index)

    # save logs
    LOGGER.export()

    # compute total collateral at end of sim
    end_total_collateral = await compute_collateral_amount()
    market = await get_perp_market_account(program, 0)
    market_collateral = market.amm.total_fee_minus_distributions / 1e6
    end_total_collateral += market_collateral

    print('---')

    usdc_spot_market = await get_spot_market_account(program, 0)

    print(
        'usdc spot market info:',
        'deposit_balance:', usdc_spot_market.deposit_balance, 
        'borrow_balance:', usdc_spot_market.borrow_balance, 
        'revenue_pool:', usdc_spot_market.revenue_pool.scaled_balance,
        'spot_fee_pool:', usdc_spot_market.spot_fee_pool.scaled_balance,
    )

    print('market $:', market_collateral)
    print(f'difference in $ {(end_total_collateral - init_total_collateral) / QUOTE_PRECISION:,}')
    print(
        "=> end/init collateral",
        (end_total_collateral, init_total_collateral)
    )

    print(
        "net baa & net unsettled:",
        market.amm.base_asset_amount_with_amm, 
        market.amm.base_asset_amount_with_unsettled_lp,
        market.amm.base_asset_amount_with_amm + market.amm.base_asset_amount_with_unsettled_lp
    )

    print(
        'net long/short',
        market.amm.base_asset_amount_long, 
        market.amm.base_asset_amount_short, 
        market.amm.user_lp_shares, 
    )

    print(
        'cumulative_social_loss / funding:',
        market.amm.cumulative_social_loss, 
        market.amm.cumulative_funding_rate_long, 
        market.amm.cumulative_funding_rate_short, 
        market.amm.last_funding_rate_long, 
        market.amm.last_funding_rate_short, 
    )

    print(
        'market pool balances:: ',
        'fee pool:', market.amm.fee_pool.scaled_balance, 
        'pnl pool:', market.pnl_pool.scaled_balance
    )

    print(
        'total time (seconds):',
        time.time() - start
    )
    
    await provider.close()
    await close_workspace(workspace)

    # export state over time with geyser to trail_outpath
    # run script with -- output path flags etc.
    import extract
    extract.main(
        protocol_path, 
        trial_outpath, 
        user_path, 
        state_path,
    )

async def main(protocol_path, experiments_folder, geyser_path, trial):
    events = pd.read_csv(f"{experiments_folder}/events.csv")

    no_oracle_guard_rails = OracleGuardRails(
        price_divergence=PriceDivergenceGuardRails(1, 1), 
        validity=ValidityGuardRails(10, 10, 100, 100),
        use_for_liquidations=True
    )

    trial_guard_rails = None
    spread = None

    if 'spread_250' in trial:
        print('=> spread 250 activated')
        spread = 250
        trial_guard_rails = no_oracle_guard_rails
   
    if 'spread_1000' in trial:
        print('=> spread 1000 activated')
        spread = 1000
        trial_guard_rails = no_oracle_guard_rails

    if 'no_oracle_guards' in trial:
        print('=> no_oracle_guard_rails activated')
        trial_guard_rails = no_oracle_guard_rails

    # read and load initial clearing house state (thats all we use chs.csv for...)
    with open(f'{experiments_folder}/markets_json.csv', 'r') as f:
        markets = json.load(f)

    experiment_name = Path(experiments_folder).stem
    output_path = Path(f'{experiments_folder}/../../results/{experiment_name}/trial_{trial}')
    output_path.mkdir(exist_ok=True, parents=True)

    setup_run_info(str(output_path), protocol_path, '')

    val = LocalValidator(protocol_path, geyser_path)
    val.start()
    try:
        await run_trial(
            protocol_path, 
            events, 
            markets, 
            output_path, 
            trial_guard_rails, 
            spread
        )
    finally:
        val.stop()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--events', type=str, required=True)
    parser.add_argument('--protocol', type=str, required=False, default='../driftpy/protocol-v2')
    parser.add_argument('--geyser', type=str, required=False, default='../solana-accountsdb-plugin-postgres')

    # trials = ['no_oracle_guards', 'spread_250', 'spread_1000', 'oracle_guards',]
    parser.add_argument('-t', '--trial', type=str, required=False, default='')

    args = parser.parse_args()

    try: 
        import asyncio
        asyncio.run(main(args.protocol, args.events, args.geyser, args.trial))
    finally:
        pass

