#! /usr/bin/env python3.9

########################################################################################################################
# Imports
########################################################################################################################

import argparse
import asyncio
from collections import defaultdict, deque, OrderedDict
from dataclasses import dataclass
from enum import IntEnum
from getpass import getpass
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Any, Callable, Generic, Optional, TypeVar, Union

import bitmex  # type: ignore[import]
from bitmex_websocket import BitMEXWebsocket  # type: ignore[import]


########################################################################################################################
# Globals
########################################################################################################################

logger = logging.getLogger(__name__)


########################################################################################################################
# Utilities
########################################################################################################################

BASE64URL_PATTERN = re.compile(r'^[-0-9A-Z_a-z]+$')
TICKER_BASIC_SYMBOL_PATTERN = re.compile(r'^[A-Z]+$')
TICKER_EXTENDED_SYMBOL_PATTERN = re.compile(r'^[A-Za-z]+$')


def assert_base64url(value: Any) -> str:
    assert isinstance(value, str) and BASE64URL_PATTERN.match(value)
    return value


def assert_bool(value: Any) -> bool:
    assert isinstance(value, bool)
    return value


def assert_diminishing_multiplier(value: Any) -> float:
    assert isinstance(value, float) and 0. < value < 1.
    return value


def assert_non_diminishing_multiplier(value: Any) -> float:
    assert isinstance(value, float) and value >= 1.
    return value


def assert_non_negative_integer(value: Any) -> int:
    assert isinstance(value, int) and value >= 0
    return value


def assert_positive_integer(value: Any) -> int:
    assert isinstance(value, int) and value > 0
    return value


def assert_positive_percentage(value: Any) -> float:
    assert isinstance(value, float) and 0. < value <= 1.
    return value


def assert_positive_real(value: Any) -> float:
    assert isinstance(value, float) and value > 0.
    return value


def assert_ticker_basic_symbol(value: Any) -> str:
    assert isinstance(value, str) and TICKER_BASIC_SYMBOL_PATTERN.match(value)
    return value


def assert_ticker_extended_symbol(value: Any) -> str:
    assert isinstance(value, str) and TICKER_EXTENDED_SYMBOL_PATTERN.match(value)
    return value


########################################################################################################################
# Types
########################################################################################################################

class Signal(IntEnum):
    BUY = 1
    SELL = -1
    CLOSE = 0
    BUYCLOSE = -1
    SELLCLOSE = 1


@dataclass(order=True, frozen=True)
class OrderBookLevel:
    # TODO: Use `@dataclass(slots=True)` once we're on Python 3.10.
    __slots__ = ('price', 'volume')

    price: float
    volume: int


@dataclass(frozen=True)
class L2OrderBookTick:
    # TODO: Use `@dataclass(slots=True)` once we're on Python 3.10.
    __slots__ = ('monotonic_timestamp_ns', 'midpoint_price', 'bids', 'asks')

    monotonic_timestamp_ns: int
    midpoint_price: float
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]


@dataclass(frozen=True)
class HackyMcHackface:
    # TODO: Use `@dataclass(slots=True)` once we're on Python 3.10.
    __slots__ = ('midpoint_price', 'best_bid', 'best_ask')

    midpoint_price: float
    best_bid: Optional[float]
    best_ask: Optional[float]


@dataclass(frozen=True)
class ExponentialMovingAverageResult:
    __slots__ = ('current_value', 'best_bid', 'best_ask', 'mean', 'variance', 'sma_mean', 'sma_variance')

    current_value: float
    best_bid: Optional[float]
    best_ask: Optional[float]
    mean: float
    variance: float
    sma_mean: float
    sma_variance: float


########################################################################################################################
# Nodes
########################################################################################################################

NCT = TypeVar('NCT')
NRT = TypeVar('NRT')


class Node(Generic[NCT, NRT]):
    __slots__ = ('engine', 'config')

    def __init__(self, engine: 'Engine', config: NCT) -> None:
        self.engine = engine
        self.config = config

    def publish_result(self, result: NRT) -> None:
        logger.debug(f'Publishing {result} from node for {self.config}.')
        self.engine.publish_node_result(self, result)


# Absolute duration (in seconds) between when a timer tick is scheduled and when it is invoked, above which a warning
# will be emitted.
TIMER_TICK_EPSILON = 0.02


@dataclass(frozen=True)
class TimerNodeConfig:
    __slots__ = ('duration',)

    duration: float

    def __post_init__(self) -> None:
        assert_positive_real(self.duration)


class TimerNode(Node[TimerNodeConfig, int]):
    __slots__ = ('epoch_timestamp', 'next_tick_timestamp', 'ticks')

    def __init__(self, engine: 'Engine', config: TimerNodeConfig) -> None:
        super().__init__(engine, config)
        self.epoch_timestamp = engine.loop.time()
        self.ticks = 0
        self.schedule_next_tick()

    def schedule_next_tick(self) -> None:
        current_timestamp = self.engine.loop.time()
        next_tick = self.ticks + 1
        self.next_tick_timestamp = self.epoch_timestamp + (self.config.duration * next_tick)
        delta = current_timestamp - self.next_tick_timestamp
        if delta > 0:
            logger.warning(f'Next tick #{next_tick} (from node for {self.config}) is late by {delta} s!')
        self.engine.loop.call_at(self.next_tick_timestamp, self.handle_tick)

    def handle_tick(self) -> None:
        current_timestamp = self.engine.loop.time()
        delta = current_timestamp - self.next_tick_timestamp
        is_late = abs(delta) > TIMER_TICK_EPSILON

        self.ticks += 1

        message = f'Scheduled tick #{self.ticks} (from node for {self.config}) invoked '
        if delta == 0:
            message += 'on time!'
        else:
            message += f'''{abs(delta)} s ({'>' if is_late else '???'} {TIMER_TICK_EPSILON} s) {'late' if delta >= 0 else 'early'}.'''
        if is_late:
            logger.warning(message)
        else:
            logger.debug(message)

        self.publish_result(self.ticks)
        self.schedule_next_tick()


@dataclass(frozen=True)
class L2OrderBookTickNodeConfig:
    __slots__ = ()


class L2OrderBookTickNode(Node[L2OrderBookTickNodeConfig, L2OrderBookTick]):
    __slots__ = ()

    def __init__(self, engine: 'Engine', config: L2OrderBookTickNodeConfig) -> None:
        super().__init__(engine, config)
        engine.loop.create_task(self.drain_queue())

    async def drain_queue(self) -> None:
        while True:
            tick = await self.engine.l2_order_book_tick_queue.get()
            self.publish_result(tick)


@dataclass(frozen=True)
class DiscretisedL2OrderBookTickNodeConfig:
    __slots__ = ('duration',)

    duration: float

    def __post_init__(self) -> None:
        assert_positive_real(self.duration)


class DiscretisedL2OrderBookTickNode(Node[DiscretisedL2OrderBookTickNodeConfig, L2OrderBookTick]):
    __slots__ = ('last_timer_tick', 'last_l2_order_book_tick')

    def __init__(self, engine: 'Engine', config: DiscretisedL2OrderBookTickNodeConfig) -> None:
        super().__init__(engine, config)
        self.last_timer_tick: Optional[int] = None
        self.last_l2_order_book_tick: Optional[L2OrderBookTick] = None
        engine.subscribe_node_result(TimerNodeConfig(duration=config.duration), self.handle_timer_tick)
        engine.subscribe_node_result(L2OrderBookTickNodeConfig(), self.handle_l2_order_book_tick)

    def handle_timer_tick(self, tick: int) -> None:
        assert self.last_timer_tick is None or tick == self.last_timer_tick + 1
        self.last_timer_tick = tick
        if self.last_l2_order_book_tick is not None:
            self.publish_result(self.last_l2_order_book_tick)

    def handle_l2_order_book_tick(self, tick: L2OrderBookTick) -> None:
        self.last_l2_order_book_tick = tick


@dataclass(frozen=True)
class MidpointPriceNodeConfig:
    __slots__ = ('l2_order_book_tick_node_config',)

    l2_order_book_tick_node_config: Union[L2OrderBookTickNodeConfig, DiscretisedL2OrderBookTickNodeConfig]


class MidpointPriceNode(Node[MidpointPriceNodeConfig, HackyMcHackface]):
    __slots__ = ()

    def __init__(self, engine: 'Engine', config: MidpointPriceNodeConfig) -> None:
        super().__init__(engine, config)
        engine.subscribe_node_result(config.l2_order_book_tick_node_config, self.handle_l2_order_book_tick)

    def handle_l2_order_book_tick(self, tick: L2OrderBookTick) -> None:
        best_bid = tick.bids[0].price if len(tick.bids) > 0 else None
        best_ask = tick.asks[0].price if len(tick.asks) > 0 else None
        self.publish_result(HackyMcHackface(midpoint_price=tick.midpoint_price, best_bid=best_bid, best_ask=best_ask))


@dataclass(frozen=True)
class ExponentialMovingAverageNodeConfig:
    __slots__ = ('node_config', 'alpha', 'sma_lookback')

    node_config: Any
    alpha: float
    sma_lookback: int

    def __post_init__(self) -> None:
        assert_diminishing_multiplier(self.alpha)
        assert_positive_integer(self.sma_lookback)


class ExponentialMovingAverageNode(Node[ExponentialMovingAverageNodeConfig, ExponentialMovingAverageResult]):
    __slots__ = ('a', 'n', 'count', 'mean', 'variance', 'values', 'sma_sum')

    def __init__(self, engine: 'Engine', config: ExponentialMovingAverageNodeConfig) -> None:
        super().__init__(engine, config)
        self.a = 1 - config.alpha
        self.n = math.ceil((2 / config.alpha) - 1)
        self.count = 0
        self.mean: Optional[float] = None
        self.variance = 0.
        self.values: deque[NRT] = deque(maxlen=config.sma_lookback)
        self.sma_sum = 0.
        engine.subscribe_node_result(config.node_config, self.handle_value)

    def handle_value(self, hacky_value: HackyMcHackface) -> None:
        value = hacky_value.midpoint_price
        self.count += 1
        if self.count == 1:
            assert self.mean is None
            self.mean = value
        else:
            assert self.mean is not None
            # See <https://fanf2.user.srcf.net/hermes/doc/antiforgery/stats.pdf>.
            delta = value - self.mean
            increment = self.config.alpha * delta
            self.mean += increment
            self.variance = self.a * (self.variance + (delta * increment))

        if len(self.values) == self.values.maxlen:
            self.sma_sum -= self.values[0]
        self.values.append(value)
        self.sma_sum += value

        if self.count >= self.n and self.count >= self.config.sma_lookback:
            sma_mean = self.sma_sum / self.config.sma_lookback
            sma_variance = sum((value - sma_mean) ** 2 for value in self.values) / (self.config.sma_lookback - 1)
            self.publish_result(ExponentialMovingAverageResult(current_value=value, best_bid=hacky_value.best_bid, best_ask=hacky_value.best_ask, mean=self.mean, variance=self.variance, sma_mean=sma_mean, sma_variance=sma_variance))


########################################################################################################################
# Engine
########################################################################################################################

@dataclass(frozen=True)
class EngineConfig:
    # TODO: Use `@dataclass(slots=True)` once we're on Python 3.10.
    __slots__ = ('symbol', 'base_currency', 'quote_currency', 'account_currency', 'settlement_currency', 'leverage', 'position_currency')

    # Ticker symbol.
    symbol: str
    # Base currency symbol.
    base_currency: str
    # Quote currency symbol.
    quote_currency: str
    # Account currency symbol. When orders are executed, we spend or receive from this wallet.
    account_currency: str
    # Settlement currency symbol. When orders are executed, we spend or receive in this currency.
    settlement_currency: str
    # Leverage ratio, for margin trading.
    leverage: float
    # Position currency symbol. When orders are executed, our position in this currency changes.
    position_currency: str

    def __post_init__(self) -> None:
        assert_ticker_basic_symbol(self.symbol)
        assert_ticker_basic_symbol(self.base_currency)
        assert_ticker_basic_symbol(self.quote_currency)
        assert f'{self.base_currency}{self.quote_currency}' == self.symbol
        assert_ticker_extended_symbol(self.account_currency)
        assert_ticker_extended_symbol(self.settlement_currency)
        assert self.settlement_currency.upper() == self.quote_currency
        assert self.settlement_currency == self.account_currency
        assert_non_diminishing_multiplier(self.leverage)
        assert_ticker_basic_symbol(self.position_currency)
        assert self.position_currency == self.base_currency


class Engine:
    __slots__ = ('config', 'loop', 'l2_order_book_tick_queue', 'node_type_registry', 'node_registry', 'node_subscriptions')

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.loop = asyncio.get_event_loop()
        self.l2_order_book_tick_queue: asyncio.Queue[L2OrderBookTick] = asyncio.Queue()
        self.node_type_registry: dict[type[Any], type[Node]] = {}
        self.node_registry: dict[Any, Node] = {}
        self.node_subscriptions: dict[Node, set[Callable[[Any], None]]] = defaultdict(set)

        self.register_node_type(TimerNodeConfig, TimerNode)
        self.register_node_type(L2OrderBookTickNodeConfig, L2OrderBookTickNode)
        self.register_node_type(DiscretisedL2OrderBookTickNodeConfig, DiscretisedL2OrderBookTickNode)
        self.register_node_type(MidpointPriceNodeConfig, MidpointPriceNode)
        self.register_node_type(ExponentialMovingAverageNodeConfig, ExponentialMovingAverageNode)

    def register_node_type(self, config_cls: type[NCT], node_cls: type[Node[NCT, Any]]) -> None:
        assert config_cls not in self.node_type_registry, f'Cannot register {config_cls} to {node_cls} as it is already registered to {self.node_type_registry[config_cls]}'
        logger.debug(f'Registering {config_cls} to {node_cls}.')
        self.node_type_registry[config_cls] = node_cls

    def get_node(self, config: NCT) -> Node[NCT, Any]:
        config_cls = type(config)
        assert config_cls in self.node_type_registry, f'Cannot get node for {config} as {config_cls} is not registered'
        if config in self.node_registry:
            logger.debug(f'Returning existing node for {config}.')
            return self.node_registry[config]
        else:
            logger.info(f'Creating new node for {config}.')
            node_cls = self.node_type_registry[config_cls]
            node = node_cls(self, config)
            self.node_registry[config] = node
            return node

    def subscribe_node_result(self, config: NCT, handler: Callable[[NRT], None]) -> None:
        node = self.get_node(config)
        handlers = self.node_subscriptions[node]
        if handler in handlers:
            logger.warning(f'{handler} is already subscribed to node for {config}!')
        else:
            logger.info(f'Subscribing {handler} to node for {config}.')
            handlers.add(handler)

    def publish_node_result(self, node: Node[Any, NRT], result: NRT) -> None:
        for handler in self.node_subscriptions[node]:
            logger.debug(f'Scheduling callback to {handler} with {result}.')
            self.loop.call_soon(handler, result)

    def run(self) -> None:
        self.loop.run_forever()


########################################################################################################################
# Exchange
########################################################################################################################

@dataclass(frozen=True)
class BitmexExchangeConfig:
    # TODO: Use `@dataclass(slots=True)` once we're on Python 3.10.
    __slots__ = ('is_live', 'api_key', 'api_secret')

    # `True` if we're trading on the live exchange, otherwise `False` if we're trading on the simulated exchange.
    is_live: bool
    # BitMEX API key ID.
    api_key: str
    # BitMEX API key secret.
    api_secret: str

    def __post_init__(self) -> None:
        assert_bool(self.is_live)
        assert_base64url(self.api_key)
        assert_base64url(self.api_secret)


class BitmexExchange:
    __slots__ = ('client', 'ws')

    def __init__(self, engine: Engine, config: BitmexExchangeConfig) -> None:
        self.client = bitmex.bitmex(
            test=not config.is_live,
            api_key=config.api_key,
            api_secret=config.api_secret,
        )

        def l2_order_book_tick_queue_putter(tick: L2OrderBookTick):
            """
            This func is assigned to self.ws.l2_order_book_tick_queue_putter so
            data passed to engine
            """
            engine.loop.call_soon_threadsafe(engine.l2_order_book_tick_queue.put_nowait, tick)

        def connect() -> None:
            self.ws = BitmexWebsocketEx(
                engine_config=engine.config,
                l2_order_book_tick_queue_putter=l2_order_book_tick_queue_putter,
                on_close=reconnect,
                endpoint=f'''wss://ws.{'' if config.is_live else 'testnet.'}bitmex.com/realtime''',
                symbol=engine.config.symbol,
                api_key=config.api_key,
                api_secret=config.api_secret,
                subscriptions=(
                    'instrument',
                    'orderBookL2',
                    'quote',
                    'trade',
                    'execution',
                    'order',
                    'margin',
                    'position',
                    'wallet',
                )
            )

        def reconnect() -> None:
            logger.warning('Websocket closed! Will reconnect.')
            connect()

        connect()


class BitmexWebsocketEx(BitMEXWebsocket):
    __slots__ = ('engine_config', 'l2_order_book_tick_queue_putter', 'on_close')

    def __init__(self, engine_config: EngineConfig, l2_order_book_tick_queue_putter: Callable[[L2OrderBookTick], None], on_close: Callable[[], None], *args, **kwargs) -> None:
        self.engine_config = engine_config
        self.l2_order_book_tick_queue_putter = l2_order_book_tick_queue_putter
        self.on_close = on_close
        super().__init__(*args, **kwargs)

    def _BitMEXWebsocket__on_message(self, message: str) -> None:
        monotonic_timestamp_ns = time.monotonic_ns()
        super()._BitMEXWebsocket__on_message(message)
        deserialised_message = json.loads(message)  # TODO: Avoid double deserialisation.
        if deserialised_message.get('action'):
            table = deserialised_message.get('table')
            if table == 'orderBookL2':
                self.__put_l2_order_book_tick_exchange_event(monotonic_timestamp_ns)

    def _BitMEXWebsocket__on_close(self) -> None:
        super()._BitMEXWebsocket__on_close()
        self.on_close()

    def __put_l2_order_book_tick_exchange_event(self, monotonic_timestamp_ns: int) -> None:
        data = self.data['orderBookL2']
        bids = sorted((
            OrderBookLevel(price=datum['price'], volume=datum['size'])
            for datum in data
            if datum['symbol'] == self.engine_config.symbol and datum['side'] == 'Buy'
        ), reverse=True)
        asks = sorted((
            OrderBookLevel(price=datum['price'], volume=datum['size'])
            for datum in data
            if datum['symbol'] == self.engine_config.symbol and datum['side'] == 'Sell'
        ))
        midpoint_price = (bids[0].price + asks[0].price) / 2
        l2_order_book_tick = L2OrderBookTick(
            monotonic_timestamp_ns=monotonic_timestamp_ns,
            midpoint_price=midpoint_price,
            bids=bids,
            asks=asks,
        )
        self.l2_order_book_tick_queue_putter(l2_order_book_tick)


########################################################################################################################
# Strategy
########################################################################################################################

@dataclass(frozen=True)
class MomentumIndicatorResult:
    __slots__ = ('midpoint_price', 'best_bid', 'best_ask', 'momentum', 'significance', 'sma_variance')

    midpoint_price: float
    best_bid: Optional[float]
    best_ask: Optional[float]
    momentum: float
    significance: float
    sma_variance: float
    


@dataclass(frozen=True)
class MomentumIndicatorNodeConfig:
    __slots__ = ('half_life')

    half_life: int

   

    def __post_init__(self) -> None:
        assert_positive_integer(self.half_life)



class MomentumIndicatorNode(Node[MomentumIndicatorNodeConfig, MomentumIndicatorResult]):
    __slots__ = ('magic_constant',)

    def __init__(self, engine: 'Engine', config: MomentumIndicatorNodeConfig) -> None:
        super().__init__(engine, config)
        self.magic_constant = 2 * math.log(2) / config.half_life
        l2_order_book_tick_node_config = DiscretisedL2OrderBookTickNodeConfig(duration=1.)
        mpp_node_config = MidpointPriceNodeConfig(l2_order_book_tick_node_config=l2_order_book_tick_node_config)
        alpha = 2 / (config.half_life + 1)
        ema_node_config = ExponentialMovingAverageNodeConfig(node_config=mpp_node_config, alpha=alpha, sma_lookback=config.half_life)
        engine.subscribe_node_result(ema_node_config, self.handle_ema_result)

    def handle_ema_result(self, ema_result: ExponentialMovingAverageResult) -> None:
        momentum = 10000
        significance = abs((ema_result.current_value - ema_result.sma_mean) / math.sqrt(ema_result.sma_variance)) if ema_result.sma_variance != 0 else 0
        sma_variance = ema_result.sma_variance
        self.publish_result(MomentumIndicatorResult(midpoint_price=ema_result.current_value, best_bid=ema_result.best_bid, best_ask=ema_result.best_ask,
                                                    momentum=momentum, significance=significance, sma_variance=sma_variance))

@dataclass(frozen=True)
class WindowedPairNodeConfig:
    __slots__ = ('node_config', 'n', 'n2', 'n3')

    node_config: Any
    n: int
    n2: int
    n3: int

    def __post_init__(self):
        assert_positive_integer(self.n)
        assert_positive_integer(self.n2)
        assert_positive_integer(self.n3)


class WindowedPairNode(Node[WindowedPairNodeConfig, tuple[NRT, NRT]]):
    __slots__ = ('values',)

    def __init__(self, engine: 'Engine', config: WindowedPairNodeConfig) -> None:
        super().__init__(engine, config)
        assert config.n3 > config.n
        self.values: deque[NRT] = deque(maxlen=config.n3)
        engine.subscribe_node_result(config.node_config, self.handle_value)

    def handle_value(self, value: NRT):
        self.values.append(value)
        if len(self.values) == self.values.maxlen:
            self.publish_result((value, self.values[self.values.maxlen - 2], self.values[1], self.values[0]))


# @dataclass
# class MomentumIndicatorHistory:
#     history: list[MomentumIndicatorResult]
#     timestamps: list[float]

#     def record(self, momentum_indicator_result: MomentumIndicatorResult) -> int:
#         self.history.append(momentum_indicator_result)
#         self.timestamps.append(time.time())
#         return len(self.history) - 1

#     def query_history_upper_or_lower(self, start_index: int, end_index: int = -1) -> Optional[bool]:
#         for cur_index in range(start_index, end_index, -1):
#             momentum: MomentumIndicatorResult = self.history[cur_index]
#             if momentum.midpoint_price > momentum.upper1:
#                 return True
#             elif momentum.midpoint_price < momentum.lower1:
#                 return False
#         return None


@dataclass(frozen=True)
class MomentumSignalNodeConfig:
    __slots__ = ('half_life', 'threshold', 'MOMENTUM_SIGNAL_SIGNIFICANCE_THRESHOLD', 'VARIANCE_THERESHOLD',
                 'symbol0', 'ordType0', 'StopLoss', 'orderQty0', 'bitmex_api_key', 'bitmex_api_secret',
                 'is_live')

    half_life: int
    threshold: float

    MOMENTUM_SIGNAL_SIGNIFICANCE_THRESHOLD: float
    VARIANCE_THERESHOLD: float
    

    symbol0: str
    ordType0: str
    StopLoss: float
    orderQty0: int
    bitmex_api_key: str
    bitmex_api_secret: str
    is_live: bool

    def __post_init__(self):
        assert_positive_integer(self.half_life)
        assert_positive_real(self.threshold)


class MomentumSignalNode(Node[MomentumSignalNodeConfig, Signal]):
    __slots__ = ('client',)

    def __init__(self, engine: 'Engine', config: MomentumSignalNodeConfig) -> None:
        super().__init__(engine, config)
        self.client = bitmex.bitmex(test=not config.is_live, api_key=config.bitmex_api_key, api_secret=config.bitmex_api_secret)

        momentum_indicator_node_config = MomentumIndicatorNodeConfig(half_life=config.half_life)
        windowed_pair_node_config = WindowedPairNodeConfig(node_config=momentum_indicator_node_config, n=2, n2=config.half_life + 1, n3=config.half_life + 2)
        engine.subscribe_node_result(windowed_pair_node_config, self.handle_momenta)
        #self.tracking_map: OrderedDict[str, str] = OrderedDict()
        #self.history = MomentumIndicatorHistory([], [])
        #self.last_query_index = -1


    def handle_momenta(self, momenta: tuple[MomentumIndicatorResult, MomentumIndicatorResult, MomentumIndicatorResult, MomentumIndicatorResult]):
        (current_momentum, previous_momentum_1, previous_momentum_2, previous_momentum_3) = momenta
        # record history
        #record_index = self.history.record(current_momentum)

        position1 = 0

        momentum0 = current_momentum.midpoint_price - previous_momentum_1.midpoint_price
        momentum1 = current_momentum.midpoint_price - previous_momentum_2.midpoint_price
        momentum2 = previous_momentum_1.midpoint_price - previous_momentum_3.midpoint_price

        momentum_difference = momentum1 - momentum2
      
        varRatio = (current_momentum.sma_variance) / (previous_momentum_2.sma_variance) if previous_momentum_2.sma_variance != 0 else 0
        partial_message = f'[Current (MPP, bid, ask, momentum0, momentum1, momentum2, momentum_difference, varRatio) is ({current_momentum.midpoint_price}, {current_momentum.best_bid},  {current_momentum.best_ask}, {momentum0}, {momentum1}, {momentum2}, {momentum_difference}, {varRatio}).]'
        
        # positions = self.client.Position.Position_get(filter=json.dumps({'symbol': self.config.symbol0})).result()[0]  # to get 'isOpen', 'currentQty'
        # positions = positions[0] if len(positions) > 0 else {'currentQty': 0

                #NewOrder = client.Order.Order_new(symbol=symbol, orderQty= - positions["currentQty"] , ordType='Market').result()
        
        if (momentum0 > 0 and momentum1 > 0 and momentum_difference > 0 and varRatio > self.config.VARIANCE_THERESHOLD):
    
            logger.info(f'{partial_message}: BUY signal!')
            self.publish_result(Signal.BUY)
            signal = Signal.BUY
            position1 += 1
            if position1 > 0:
                if current_momentum.midpoint_price <= previous_momentum_1.midpoint_price:
                    longStop = max(longStop, previous_momentum_1.midpoint_price *0.97)
                    logger.info(f'Long stop = {longStop}')
                else:
                    longStop = current_momentum.midpoint_price *0.97
                    logger.info(f'Long stop = {longStop}')
                if current_momentum.midpoint_price < longStop:
                    # trailingstop = True
                    logger.info(f'Long stop = {longStop}')
        elif (momentum0 < 0 and momentum1 < 0 and momentum_difference < 0 and varRatio > self.config.VARIANCE_THERESHOLD):      
            logger.info(f'{partial_message}: SELL signal!')
            self.publish_result(Signal.SELL)
            signal = Signal.SELL
            position1 -= 1
            if position1 < 0:
                if current_momentum.midpoint_price >= previous_momentum_1.midpoint_price:
                    shortStop = min(shortStop, previous_momentum.midpoint_price *1.03)
                    logger.info(f'Short stop = {shortStop}')
                if current_momentum.midpoint_price < previous_momentum_1.midpoint_price:
                    shortStop = current_momentum.midpoint_price * 1.03
                    logger.info(f'Short stop = {shortStop}')
                if current_momentum.midpoint_price > shortStop:
                    logger.info(f'Short stop = {shortStop}')
                #    logger.info(f'Short stop = {shortStop}')
                elif (position1 == 0):
                    longStop = 0
                    shortStop = 9999999
                    trailingstop = False
        else:
            logger.info(f'{partial_message}: No signal (price within threshold).')
            return





@dataclass(frozen=True)
class MomentumStrategyConfig:
    # TODO: Use `@dataclass(slots=True)` once we're on Python 3.10.
    __slots__ = ('half_life', 'threshold', 'per_trade_usage', 'signal_execute_config')

    # Look-back duration (in seconds).
    half_life: int
    # Ratio of current momentum to look-back momentum above which a buy or sell signal may be emitted.
    threshold: float
    # Percentage of available balance (including margin) to use when a buy or sell signal is emitted.
    per_trade_usage: float

    signal_execute_config: dict[str, int | str | bool]

    def __post_init__(self) -> None:
        assert_positive_integer(self.half_life)
        assert_positive_real(self.threshold)
        assert_positive_percentage(self.per_trade_usage)


class MomentumStrategy:
    __slots__ = ()

    def __init__(self, engine: Engine, config: MomentumStrategyConfig) -> None:
        engine.register_node_type(MomentumIndicatorNodeConfig, MomentumIndicatorNode)
        engine.register_node_type(WindowedPairNodeConfig, WindowedPairNode)
        engine.register_node_type(MomentumSignalNodeConfig, MomentumSignalNode)

        signal_execute_config = config.signal_execute_config
        signal_execute_config.update(half_life=config.half_life, threshold=config.threshold)
        engine.get_node(MomentumSignalNodeConfig(**signal_execute_config))  # TODO


########################################################################################################################
# Execution
########################################################################################################################

@dataclass(frozen=True)
class WorkTheBidExecutionConfig:
    # TODO: Use `@dataclass(slots=True)` once we're on Python 3.10.
    __slots__ = ('max_depth', 'min_trade_lots', 'order_ratio', 'renew_after_duration', 'renew_after_trade_lots')

    # Maximum order book depth (in levels) to which orders can be placed.
    max_depth: int
    # Minimum trade size (in lots) per level.
    min_trade_lots: int
    # Target percentage of volume at the level the order will be placed. This will be rounded down to the nearest lot.
    # This may then be adjusted up to the minimum trade size, or down if we're on our last order.
    order_ratio: float
    # Duration (in seconds) that can elapse with an order not being completely fulfilled, after which all open orders
    # will be re-evaluated.
    renew_after_duration: float
    # Volume of trades (in lots) that can elapse with an order not being completely fulfilled, after which all open
    # orders will be re-evaluated.
    renew_after_trade_lots: int

    def __post_init__(self) -> None:
        assert_positive_integer(self.max_depth)
        assert_positive_integer(self.min_trade_lots)
        assert_positive_percentage(self.order_ratio)
        assert_positive_real(self.renew_after_duration)
        assert_positive_integer(self.renew_after_trade_lots)


########################################################################################################################
# Bootstrap
########################################################################################################################

def main() -> None:
    (verbosity, log_file, engine_config, exchange_config, strategy_config, execution_config) = get_config()
    init_logging(verbosity, log_file)

    engine = Engine(engine_config)
    exchange = BitmexExchange(engine, exchange_config)
    strategy = MomentumStrategy(engine, strategy_config)
    engine.run()


def get_config() -> tuple[int, Optional[Path], EngineConfig, BitmexExchangeConfig, MomentumStrategyConfig, WorkTheBidExecutionConfig]:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', nargs='?', default='config.json', type=argparse.FileType('r'))
    parser.add_argument('--verbose', '-v', action='count', default=0, dest='verbosity')
    parser.add_argument('--log-file', nargs='?', default='record.log', type=Path)  # output log content to ./record.log by default
    args = parser.parse_args()

    config = json.load(args.config)
    engine_config = EngineConfig(
        symbol=config['symbol'],
        base_currency=config['baseCurrency'],
        quote_currency=config['quoteCurrency'],
        account_currency=config['accountCurrency'],
        settlement_currency=config['settlementCurrency'],
        leverage=config['leverage'],
        position_currency=config['positionCurrency'],
    )
    exchange_config = BitmexExchangeConfig(
        is_live=config['exchange']['isLive'],
        api_key=config['exchange']['apiKey'],
        api_secret=getpass(f'''Enter BitMEX API key secret (for ID `{config['exchange']['apiKey']}`): ''')  # ask for secret input from terminal
        # api_secret=config['exchange']['apiSecret']
    )
    strategy_config = MomentumStrategyConfig(
        half_life=config['strategy']['halfLife'],
        threshold=config['strategy']['threshold'],
        per_trade_usage=config['strategy']['perTradeUsage'],
        signal_execute_config=config['strategy']['signal_execute_config'],
    )
    execution_config = WorkTheBidExecutionConfig(
        max_depth=config['execution']['maxDepth'],
        min_trade_lots=config['execution']['minTradeLots'],
        order_ratio=config['execution']['orderRatio'],
        renew_after_duration=config['execution']['renewAfterDuration'],
        renew_after_trade_lots=config['execution']['renewAfterTradeLots'],
    )

    verbosity = assert_non_negative_integer(args.verbosity)
    return (verbosity, args.log_file, engine_config, exchange_config, strategy_config, execution_config)


def init_logging(verbosity: int, log_file: Optional[Path]) -> None:
    formatter = logging.Formatter('[{asctime}] [{levelname:<8s}] [{threadName:s}] [{funcName:s}]: {message:s}', style='{')

    root_stream_handler = logging.StreamHandler()
    root_stream_handler.setLevel(logging.DEBUG)
    root_stream_handler.setFormatter(formatter)

    if log_file:
        root_file_handler = logging.FileHandler(filename=log_file, mode='a', encoding='utf-8')
        root_file_handler.setLevel(logging.DEBUG)
        root_file_handler.setFormatter(formatter)

    # By default, log `WARNING`s and higher.
    # If `-v`, log `INFO`s and higher.
    # If `-vv`, log everything for the core application, and `INFO`s and higher for everything else.
    # If `-vvv`, log everything.
    logger.setLevel(logging.DEBUG if verbosity == 2 else logging.NOTSET)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if verbosity >= 3 else logging.INFO if verbosity >= 1 else logging.WARNING)
    root_logger.addHandler(root_stream_handler)
    if log_file:
        root_logger.addHandler(root_file_handler)


if __name__ == '__main__':
    main()