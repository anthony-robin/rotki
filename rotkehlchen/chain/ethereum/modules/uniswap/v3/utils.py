import logging
from typing import TYPE_CHECKING, Any, Dict, List, Set, Tuple

from eth_abi import encode_abi
from eth_abi.packed import encode_abi_packed
from eth_utils import to_checksum_address
from web3 import Web3

from rotkehlchen.accounting.structures.balance import Balance
from rotkehlchen.assets.asset import EthereumToken
from rotkehlchen.assets.utils import get_or_create_ethereum_token
from rotkehlchen.chain.ethereum.contracts import EthereumContract
from rotkehlchen.chain.ethereum.interfaces.ammswap.types import LiquidityPoolAsset
from rotkehlchen.chain.ethereum.interfaces.ammswap.utils import TokenDetails
from rotkehlchen.chain.ethereum.modules.uniswap.v3.types import NFTLiquidityPool
from rotkehlchen.chain.ethereum.utils import multicall_2
from rotkehlchen.constants.ethereum import (
    UNISWAP_V3_FACTORY,
    UNISWAP_V3_NFT_MANAGER,
    UNISWAP_V3_POOL_ABI,
)
from rotkehlchen.constants.misc import ZERO
from rotkehlchen.errors.misc import NotERC20Conformant, RemoteError
from rotkehlchen.fval import FVal
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.types import ChecksumEthAddress
from rotkehlchen.utils.misc import get_chunks

if TYPE_CHECKING:
    from rotkehlchen.chain.ethereum.manager import EthereumManager
    from rotkehlchen.db.dbhandler import DBHandler


logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)

UNISWAP_V3_POSITIONS_PER_CHUNK = 45
POOL_INIT_CODE_HASH = '0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54'
UNISWAP_V3_ERROR_MSG = 'Remote error calling multicall contract for uniswap v3 {} for address properties: {}'  # noqa: 501
POW_96 = 2**96


def uniswap_v3_lp_token_balances(
    userdb: 'DBHandler',
    address: ChecksumEthAddress,
    ethereum: 'EthereumManager',
    price_known_assets: Set[EthereumToken],
    price_unknown_assets: Set[EthereumToken],
) -> List[NFTLiquidityPool]:
    """
    Fetches all the Uniswap V3 LP positions for the specified address.
    1. Use the NFT Positions contract to call the `balanceOf` method to get number of positions.
    2. Loop through from 0 to (positions - 1) using the index and address to call
    `tokenOfOwnerByIndex` method which gives the NFT ID that represents a LP position.
    3. Use the token ID gotten above to call the `positions` method to get the current state of the
    liquidity position.
    4. Use the `positions` data to generate the LP address using the `compute_pool_address` method.
    5. Use the pool contract of the addresses generate and call the `slot0` method to get the
    LP state.
    6. Get basic information of the tokens in the LP pairs.
    7. Calculate the price ranges for which the LP position is valid for.
    8. Calculate the amount of tokens in the LP position.
    9. Calculate the amount of tokens in the LP.

    If the multicall fails due to `RemoteError` or one of the calls isn't successful, it is omitted
    from the chunk.
    """
    nft_manager_contract = EthereumContract(
        address=UNISWAP_V3_NFT_MANAGER.address,
        abi=UNISWAP_V3_NFT_MANAGER.abi,
        deployed_block=UNISWAP_V3_NFT_MANAGER.deployed_block,
    )
    balances: List[NFTLiquidityPool] = []
    try:
        my_positions = nft_manager_contract.call(
            ethereum=ethereum,
            method_name='balanceOf',
            arguments=[address],
        )
    except RemoteError as e:
        log.error(
            'Remote error calling nft manager contract to fetch of LP positions count for ',
            f'an address with properties: {str(e)}',
        )
        return balances

    if my_positions == 0:
        return balances

    chunks = list(get_chunks(list(range(my_positions)), n=UNISWAP_V3_POSITIONS_PER_CHUNK))
    for chunk in chunks:
        try:
            # Get tokens IDs from the Positions NFT contract using the user address and
            # the indexes i.e from 0 to (total number of user positions - 1)
            tokens_ids_multicall = multicall_2(
                ethereum=ethereum,
                require_success=False,
                calls=[
                    (
                        UNISWAP_V3_NFT_MANAGER.address,
                        nft_manager_contract.encode('tokenOfOwnerByIndex', [address, index]),
                    )
                    for index in chunk
                ],
            )
        except RemoteError as e:
            log.error(UNISWAP_V3_ERROR_MSG.format('nft contract token ids', str(e)))
            continue

        tokens_ids = [
            nft_manager_contract.decode(   # pylint: disable=unsubscriptable-object
                result=data[1],
                method_name='tokenOfOwnerByIndex',
                arguments=[address, index],
            )[0]
            for index, data in enumerate(tokens_ids_multicall) if data[0] is True
        ]
        try:
            # Get the user liquidity position using the token ID retrieved.
            positions_multicall = multicall_2(
                ethereum=ethereum,
                require_success=False,
                calls=[
                    (
                        UNISWAP_V3_NFT_MANAGER.address,
                        nft_manager_contract.encode('positions', [token_id]),
                    )
                    for token_id in tokens_ids
                ],
            )
        except RemoteError as e:
            log.error(UNISWAP_V3_ERROR_MSG.format('nft contract positions', str(e)))
            continue
        positions = [
            nft_manager_contract.decode(
                result=data[1],
                method_name='positions',
                arguments=[tokens_ids[index]],
            )
            for index, data in enumerate(positions_multicall) if data[0] is True
        ]
        # Generate the LP contract address with CREATE2 opcode replicated in Python using
        # factory_address, token_0, token1 and the fee of the LP all gotten from the position.
        pool_addresses = [
            compute_pool_address(
                token0_address_raw=position[2],
                token1_address_raw=position[3],
                fee=position[4],
            )
            for position in positions
        ]
        pool_contracts = [
            EthereumContract(
                address=pool_address,
                abi=UNISWAP_V3_POOL_ABI,
                deployed_block=UNISWAP_V3_FACTORY.deployed_block,
            )
            for pool_address in pool_addresses
        ]
        try:
            # Get the liquidity pool's state i.e `slot0` by iterating through
            # a pair of the LP address and its contract and reading the `slot0`
            slots_0_multicall = multicall_2(
                ethereum=ethereum,
                require_success=False,
                calls=[
                    (entry[0], entry[1].encode('slot0'))
                    for entry in zip(pool_addresses, pool_contracts)
                ],
            )
        except RemoteError as e:
            log.error(UNISWAP_V3_ERROR_MSG.format('pool contract slot0', str(e)))
            continue
        slots_0 = [
            entry[0].decode(entry[1][1], 'slot0')
            for entry in zip(pool_contracts, slots_0_multicall) if entry[1][0] is True
        ]
        tokens_a = []
        tokens_b = []
        for position in positions:
            tokens_a.append(ethereum.get_basic_contract_info(to_checksum_address(position[2])))
            tokens_b.append(ethereum.get_basic_contract_info(to_checksum_address(position[3])))
        # Get the ranges of price for which each position is valid for.
        # Get the amount of each token present in an LP position.
        price_ranges = []
        amounts_0 = []
        amounts_1 = []
        for entry in zip(positions, slots_0, tokens_a, tokens_b):
            price_ranges.append(
                calculate_price_range(
                    tick_lower=entry[0][5],
                    tick_upper=entry[0][6],
                    decimal_0=entry[2]['decimals'],
                    decimal_1=entry[3]['decimals'],
                ),
            )
            amounts_0.append(
                calculate_amount(
                    tick_lower=entry[0][5],
                    liquidity=entry[0][7],
                    tick_upper=entry[0][6],
                    decimals=entry[2]['decimals'],
                    tick=entry[1][1],
                    token_position=0,
                ),
            )
            amounts_1.append(
                calculate_amount(
                    tick_lower=entry[0][5],
                    liquidity=entry[0][7],
                    tick_upper=entry[0][6],
                    decimals=entry[3]['decimals'],
                    tick=entry[1][1],
                    token_position=1,
                ),
            )
        # First, get the total liquidity of the LPs.
        # Use the value of the liquidity to get the total amount of tokens in LPs.
        total_tokens_in_pools = []
        try:
            liquidity_in_pools_multicall = multicall_2(
                ethereum=ethereum,
                require_success=False,
                calls=[
                    (entry[0], entry[1].encode('liquidity'))
                    for entry in zip(pool_addresses, pool_contracts)
                ],
            )
        except RemoteError as e:
            log.error(UNISWAP_V3_ERROR_MSG.format('pool contract liquidity', str(e)))
            continue
        for _entry in zip(
            pool_contracts,
            liquidity_in_pools_multicall,
            positions,
            slots_0,
            tokens_a,
            tokens_b,
        ):
            liquidity_in_pool = _entry[0].decode(_entry[1][1], 'liquidity')[0]
            total_tokens_in_pools.append(
                calculate_total_amounts_of_tokens(
                    liquidity=liquidity_in_pool,
                    tick=_entry[3][1],
                    fee=_entry[2][4],
                    decimal_0=_entry[4]['decimals'],
                    decimal_1=_entry[5]['decimals'],
                ),
            )
        for item in zip(
            tokens_ids,
            pool_addresses,
            positions,
            price_ranges,
            tokens_a,
            tokens_b,
            amounts_0,
            amounts_1,
            total_tokens_in_pools,
        ):
            if FVal(item[6]) > ZERO or FVal(item[7]) > ZERO:
                item[4].update({
                    'amount': item[6],
                    'address': item[2][2],
                    'total_amount': item[8][0],
                })
                item[5].update({
                    'amount': item[7],
                    'address': item[2][3],
                    'total_amount': item[8][1],
                })
                balances.append(_decode_uniswap_v3_result(userdb, item, price_known_assets, price_unknown_assets))  # noqa: 501
    return balances


def compute_pool_address(
    token0_address_raw: str,
    token1_address_raw: str,
    fee: int,
) -> ChecksumEthAddress:
    """
    Generate the pool address from the Uniswap Factory Address, pair of tokens
    and the fee using CREATE2 opcode.
    """
    token_0 = to_checksum_address(token0_address_raw)
    token_1 = to_checksum_address(token1_address_raw)
    parameters = []
    # Sort the addresses
    if int(token_0, 16) < int(token_1, 16):
        parameters = [token_0, token_1, fee]
    else:
        parameters = [token_1, token_0, fee]
    abi_encoded_1 = encode_abi(
        ['address', 'address', 'uint24'],
        parameters,
    )
    # pylint: disable=no-value-for-parameter
    salt = Web3.solidityKeccak(abi_types=['bytes'], values=['0x' + abi_encoded_1.hex()])
    abi_encoded_2 = encode_abi_packed(['address', 'bytes32'], (UNISWAP_V3_FACTORY.address, salt))
    # pylint: disable=no-value-for-parameter
    raw_address_bytes = Web3.solidityKeccak(
        abi_types=['bytes', 'bytes'],
        values=['0xff' + abi_encoded_2.hex(), POOL_INIT_CODE_HASH],
    )
    address = to_checksum_address(raw_address_bytes[12:].hex())
    return address


def calculate_price_range(
    tick_lower: int,
    tick_upper: int,
    decimal_0: int,
    decimal_1: int,
) -> Tuple[FVal, FVal]:
    """Calculates the price range for a Uniswap V3 LP position."""
    sqrt_a = FVal(1.0001)**tick_lower
    sqrt_b = FVal(1.0001)**tick_upper

    sqrt_adjusted_a = sqrt_a * FVal(10**(decimal_0 - decimal_1))
    sqrt_adjusted_b = sqrt_b * FVal(10**(decimal_0 - decimal_1))

    return FVal(1 / sqrt_adjusted_b), FVal(1 / sqrt_adjusted_a)


def compute_sqrt_values_for_amounts(
    tick_lower: int,
    tick_upper: int,
    tick: int,
) -> Tuple[FVal, FVal, FVal]:
    """Computes the values for `sqrt`, `sqrt_a`, sqrt_b`"""
    sqrt_a = FVal(1.0001)**FVal(tick_lower / 2) * POW_96
    sqrt_b = FVal(1.0001)**FVal(tick_upper / 2) * POW_96
    sqrt = FVal(1.0001)**FVal(tick / 2) * POW_96
    sqrt = max(min(sqrt, sqrt_b), sqrt_a)

    return sqrt, sqrt_a, sqrt_b


def calculate_amount(
    tick_lower: int,
    liquidity: int,
    tick_upper: int,
    decimals: int,
    tick: int,
    token_position: int,
) -> FVal:
    """
    Calculates the amount of a token in the Uniswap V3 LP position.
    `token_position` can either be 0 or 1 representing the position of the token in a pair.
    """
    sqrt, sqrt_a, sqrt_b = compute_sqrt_values_for_amounts(
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        tick=tick,
    )
    if token_position == 0:
        amount = (liquidity * POW_96 * (sqrt_b - sqrt) / (sqrt_b * sqrt)) / 10**decimals
    elif token_position == 1:
        amount = liquidity * (sqrt - sqrt_a) / POW_96 / 10**decimals

    return FVal(amount)


def calculate_total_amounts_of_tokens(
    liquidity: int,
    tick: int,
    fee: int,
    decimal_0: int,
    decimal_1: int,
) -> Tuple[FVal, FVal]:
    """
    Calculates the total amount of tokens present in a liquidity pool.
    A fee of 10000 represents 200 ticks spacing, 3000 represents 60 ticks spacing and
    500 represents 10 ticks spacing.
    """
    if fee == 10000:
        tick_a = tick - (tick % 200)
        tick_b = tick + 200
    elif fee == 3000:
        tick_a = tick - (tick % 60)
        tick_b = tick + 60
    elif fee == 500:
        tick_a = tick - (tick % 10)
        tick_b = tick + 10

    sqrt_a = FVal(1.0001)**FVal(tick_a / 2) * POW_96
    sqrt_b = FVal(1.0001)**FVal(tick_b / 2) * POW_96
    total_amount_0 = ((liquidity * POW_96 * (sqrt_b - sqrt_a) / sqrt_b / sqrt_a) / 10**decimal_0)
    total_amount_1 = liquidity * (sqrt_b - sqrt_a) / POW_96 / 10**decimal_1

    return FVal(total_amount_0), FVal(total_amount_1)


def _decode_uniswap_v3_token(entry: Dict[str, Any]) -> TokenDetails:
    return TokenDetails(
        address=entry['address'],
        name=entry['name'],
        symbol=entry['symbol'],
        decimals=entry['decimals'],
        amount=FVal(entry['amount']),
    )


def _decode_uniswap_v3_result(
        userdb: 'DBHandler',
        data: Tuple,
        price_known_assets: Set[EthereumToken],
        price_unknown_assets: Set[EthereumToken],
) -> NFTLiquidityPool:
    """
    Takes the data aggregated from the Positions NFT contract & LP contract and converts it
    into an `NFTLiquidityPool` which is a representation of a Uniswap V3 LP position.

    Edge cases whereby a token does not conform to ERC20 standard,the user balance is set to ZERO.
    """
    nft_id = data[0]
    pool_token = data[1]
    token0 = _decode_uniswap_v3_token(data[4])
    token1 = _decode_uniswap_v3_token(data[5])
    total_amounts_of_tokens = {
        token0.address: data[4]['total_amount'],
        token1.address: data[5]['total_amount'],
    }

    assets = []
    for token in (token0, token1):
        # Set the asset balance to ZERO if the asset raises `NotERC20Conformant` exception
        asset_balance = ZERO
        try:
            asset = get_or_create_ethereum_token(
                userdb=userdb,
                symbol=token.symbol,
                ethereum_address=token.address,
                name=token.name,
                decimals=token.decimals,
            )
            asset_balance = token.amount
        except NotERC20Conformant as e:
            log.error(
                f'Error fetching ethereum token {str(token.address)} while decoding Uniswap V3 LP '
                f'position due to: {str(e)}',
            )
        # Classify the asset either as price known or unknown
        if asset.has_oracle():
            price_known_assets.add(asset)
        else:
            price_unknown_assets.add(asset)
        assets.append(LiquidityPoolAsset(
            asset=asset,
            total_amount=total_amounts_of_tokens[token.address],
            user_balance=Balance(amount=asset_balance),
        ))
    # total_supply is None because Uniswap V3 LP does not represent positions as tokens.
    pool = NFTLiquidityPool(
        address=pool_token,
        price_range=(FVal(data[3][0]), FVal(data[3][1])),
        nft_id=nft_id,
        assets=assets,
        total_supply=None,
        user_balance=Balance(amount=ZERO),
    )
    return pool