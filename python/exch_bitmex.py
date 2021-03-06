import time
import threading
import json
from functools import partial
from datetime import datetime
from ws_api_socket import WebSocketApiClient
from market_data import L2Depth, Trade
from exchange import ExchangeGateway
from instrument import Instrument
from util import Logger


class ExchGwBitmexWs(WebSocketApiClient):
    """
    Exchange socket
    """
    def __init__(self):
        """
        Constructor
        """
        WebSocketApiClient.__init__(self, 'ExchGwBitMEX')
            
    @classmethod
    def parse_l2_depth(cls, instmt, raw):
        """
        Parse raw data to L2 depth
        :param instmt: Instrument
        :param raw: Raw data in JSON
        """
        l2_depth = instmt.get_l2_depth()
        field_map = instmt.get_order_book_fields_mapping()
        for key, value in raw.items():
            if key in field_map.keys():
                try:
                    field = field_map[key]
                except:
                    print("Error from order_book_fields_mapping on key %s" % key)
                    raise
                
                if field == 'TIMESTAMP':
                    l2_depth.date_time = value.replace('T', ' ').replace('Z', '').replace('-' , '')
                elif field == 'BIDS':
                    bids = sorted(value, key=lambda x: x[0], reverse=True)
                    for i in range(0, 10):
                        l2_depth.bids[i].price = float(bids[i][0]) if type(bids[i][0]) != float else bids[i][0]
                        l2_depth.bids[i].volume = float(bids[i][1]) if type(bids[i][1]) != float else bids[i][1]
                elif field == 'ASKS':
                    asks = sorted(value, key=lambda x: x[0])
                    for i in range(0, 10):
                        l2_depth.asks[i].price = float(asks[i][0]) if type(asks[i][0]) != float else asks[i][0]
                        l2_depth.asks[i].volume = float(asks[i][1]) if type(asks[i][1]) != float else asks[i][1]
                else:
                    raise Exception('The field <%s> is not found' % field)

        return l2_depth

    @classmethod
    def parse_trade(cls, instmt, raw):
        """
        :param instmt: Instrument
        :param raw: Raw data in JSON
        :return:
        """
        trade = Trade()
        field_map = instmt.get_trades_fields_mapping()
        for key, value in raw.items():
            if key in field_map.keys():
                try:
                    field = field_map[key]
                except:
                    print("Error from trades_fields_mapping on key %s" % key)
                    raise
                
                if field == 'TIMESTAMP':
                    trade.date_time = value.replace('T', ' ').replace('Z', '')
                elif field == 'TRADE_SIDE':
                    trade.trade_side = Trade.parse_side(value)
                    if trade.trade_side == Trade.Side.NONE:
                        raise Exception('Unexpected trade side value %d' % value)
                elif field == 'TRADE_ID':
                    trade.trade_id = value
                elif field == 'TRADE_PRICE':
                    trade.trade_price = value
                elif field == 'TRADE_VOLUME':
                    trade.trade_volume = value
                else:
                    raise Exception('The field <%s> is not found' % field)        

        return trade


class ExchGwBitmex(ExchangeGateway):
    """
    Exchange gateway
    """
    def __init__(self, db_client):
        """
        Constructor
        :param db_client: Database client
        """
        ExchangeGateway.__init__(self, ExchGwBitmexWs(), db_client)

    @classmethod
    def get_exchange_name(cls):
        """
        Get exchange name
        :return: Exchange name string
        """
        return 'BitMEX'

    def on_open_handler(self, instmt, ws):
        """
        Socket on open handler
        :param instmt: Instrument
        :param ws: Web socket
        """
        Logger.info(self.__class__.__name__, "Instrument %s is subscribed in channel %s" % \
                  (instmt.get_instmt_code(), instmt.get_exchange_name()))
        if not instmt.get_subscribed():
            ws.send("{\"op\":\"subscribe\", \"args\": [\"orderBook10:%s\"]}" % instmt.get_instmt_code())
            ws.send("{\"op\":\"subscribe\", \"args\": [\"trade:%s\"]}" % instmt.get_instmt_code())
            instmt.set_subscribed(True)

    def on_close_handler(self, instmt, ws):
        """
        Socket on close handler
        :param instmt: Instrument
        :param ws: Web socket
        """
        Logger.info(self.__class__.__name__, "Instrument %s is subscribed in channel %s" % \
                  (instmt.get_instmt_code(), instmt.get_exchange_name()))
        instmt.set_subscribed(False)

    def on_message_handler(self, instmt, message):
        """
        Incoming message handler
        :param instmt: Instrument
        :param message: Message
        """
        message = json.loads(message)
        keys = message.keys()
        if 'info' in keys:
            Logger.info(self.__class__.__name__, message['info'])
        elif 'subscribe' in keys:
            Logger.info(self.__class__.__name__, 'Subscription of %s is %s' % \
                        (message['request']['args'], \
                         'successful' if message['success'] else 'failed'))
        elif 'table' in keys:
            if message['table'] == 'trade':
                for trade_raw in message['data']:
                    if trade_raw["symbol"] == instmt.get_instmt_code():
                        # Filter out the initial subscriptions
                        trade = self.api_socket.parse_trade(instmt, trade_raw)
                        if trade.trade_id != instmt.get_exch_trade_id():
                            instmt.incr_trade_id()
                            instmt.set_exch_trade_id(trade.trade_id)
                            self.db_client.insert(table=instmt.get_trades_table_name(),
                                                  columns=['id']+Trade.columns(),
                                                  values=[instmt.get_trade_id()]+trade.values())
            elif message['table'] == 'orderBook10':
                for data in message['data']:
                    if data["symbol"] == instmt.get_instmt_code():
                        instmt.set_prev_l2_depth(instmt.get_l2_depth().copy())
                        self.api_socket.parse_l2_depth(instmt, data)
                        if instmt.get_l2_depth().is_diff(instmt.get_prev_l2_depth()):
                            instmt.incr_order_book_id()
                            self.db_client.insert(table=instmt.get_order_book_table_name(),
                                                    columns=['id']+L2Depth.columns(),
                                                    values=[instmt.get_order_book_id()]+instmt.get_l2_depth().values())
            else:
                Logger.info(self.__class__.__name__, json.dumps(message,indent=2))
        else:
            Logger.error(self.__class__.__name__, "Unrecognised message:\n" + json.dumps(message))

    def start(self, instmt):
        """
        Start the exchange gateway
        :param instmt: Instrument
        :return List of threads
        """
        instmt.set_l2_depth(L2Depth(10))
        instmt.set_prev_l2_depth(L2Depth(10))
        instmt.set_order_book_table_name(self.get_order_book_table_name(instmt.get_exchange_name(),
                                                                       instmt.get_instmt_name()))
        instmt.set_trades_table_name(self.get_trades_table_name(instmt.get_exchange_name(),
                                                               instmt.get_instmt_name()))
        instmt.set_order_book_id(self.get_order_book_init(instmt))
        trade_id, last_exch_trade_id = self.get_trades_init(instmt)
        instmt.set_trade_id(trade_id)
        instmt.set_exch_trade_id(last_exch_trade_id)
        return [self.api_socket.connect(instmt.get_link(),
                                        on_message_handler=partial(self.on_message_handler, instmt),
                                        on_open_handler=partial(self.on_open_handler, instmt),
                                        on_close_handler=partial(self.on_close_handler, instmt))]

